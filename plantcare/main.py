import asyncio
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urlencode
from uuid import uuid4

import anthropic
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import care, db, notify
from .i18n import TRANSLATIONS, format_date, format_month, get_language, plural
from .identify import IdentificationError, describe, diagnose, identify
from .models import LANGUAGES, Diagnosis, PlantIdentification

BASE_DIR = Path(__file__).parent
ALLOWED_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ASPCA_URL = "https://www.aspca.org/pet-care/animal-poison-control/toxic-and-non-toxic-plants"
CALENDAR_DAYS = 28
ICS_DAYS = 90
SITTER_MAX_DAYS = 62
FAR_AWAY_DAYS = 45  # au-delà, on affiche un mois plutôt qu'un nombre de jours

db.init_storage()


def _load_secret() -> str:
    if secret := os.environ.get("PLANTCARE_SECRET"):
        return secret
    path = db.DATA_DIR / "secret.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


# Authentification optionnelle : désactivée tant que PLANTCARE_PASSWORD est vide
PASSWORD = os.environ.get("PLANTCARE_PASSWORD", "")
SECRET = _load_secret()
AUTH_COOKIE = "plantcare_auth"
# Le flux iCal et la page de garde sont ouverts sans cookie : ils sont protégés par un jeton dans l'URL
PUBLIC_PREFIXES = ("/login", "/static/", "/lang/", "/calendar.ics", "/sitter", "/sw.js")
ONE_YEAR = 365 * 24 * 3600


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(notify.scheduler()) if notify.enabled() else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="PlantCare", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/photos", StaticFiles(directory=db.PHOTOS_DIR), name="photos")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------- Authentification ----------

def _sign(purpose: str) -> str:
    return hmac.new(SECRET.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def _auth_token() -> str:
    # Inclut le mot de passe : le changer déconnecte toutes les sessions
    return _sign(f"session:{PASSWORD}")


def _calendar_token() -> str:
    return _sign("calendar")[:32]


def _sitter_token(start: date, end: date) -> str:
    # Lié aux dates : un lien de garde ne donne accès qu'à sa période
    return _sign(f"sitter:{start}:{end}")[:32]


@app.middleware("http")
async def require_login(request: Request, call_next):
    if PASSWORD and not request.url.path.startswith(PUBLIC_PREFIXES):
        if not hmac.compare_digest(request.cookies.get(AUTH_COOKIE, ""), _auth_token()):
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/login")
def login_page(request: Request, error: str | None = None):
    if not PASSWORD:
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", {"error": error})


@app.post("/login")
def login(password: str = Form("")):
    if not PASSWORD or not hmac.compare_digest(password.encode(), PASSWORD.encode()):
        return _redirect_with_error("/login", "wrong_password")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(AUTH_COOKIE, _auth_token(), max_age=ONE_YEAR, httponly=True, samesite="lax")
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE)
    return response


# ---------- Outils de rendu ----------

def _render(request: Request, name: str, context: dict, lang: str | None = None):
    lang = lang or get_language(request)
    t = TRANSLATIONS[lang]
    error = context.get("error")
    notice = request.query_params.get("notice")
    context.update({
        "lang": lang,
        "t": t,
        "error_message": t["errors"].get(error) if error else None,
        "notice_message": t["notices"].get(notice) if notice else None,
        "auth_enabled": bool(PASSWORD),
        "current_path": request.url.path,
    })
    return templates.TemplateResponse(request, name, context)


def _redirect_with_error(path: str, code: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?{urlencode({'error': code})}", status_code=303)


def _safe_next(next_path: str, fallback: str) -> str:
    # Uniquement des chemins internes, pour ne pas servir de redirection ouverte
    return next_path if next_path.startswith("/") and not next_path.startswith("//") else fallback


def _care_status(kind: str, due: date | None, lang: str) -> dict:
    t = TRANSLATIONS[lang]["care"][kind]
    if due is None:
        return {"state": "none", "label": t["never"], "days_left": None, "due": None}
    days_left = (due - date.today()).days
    if days_left < 0:
        label, state = plural(t["overdue"], -days_left), "overdue"
    elif days_left == 0:
        label, state = t["due_today"], "due"
    elif days_left > FAR_AWAY_DAYS:
        label, state = t["due_on"].format(date=format_month(due, lang)), "ok"
    else:
        label, state = plural(t["due_in"], days_left), "ok"
    return {"state": state, "label": label, "days_left": days_left, "due": due}


def _plant_view(plant: dict, lang: str) -> dict:
    plant_sheet = care.sheet(plant)
    texts = getattr(plant_sheet.texts, lang) if plant_sheet else None
    statuses = {
        kind: _care_status(kind, care.next_due(plant, plant_sheet, kind) if plant_sheet else None, lang)
        for kind in db.CARE_KINDS
    }
    return {
        **plant,
        "sheet": plant_sheet,
        "care": plant_sheet.care if plant_sheet else None,
        "texts": texts,
        "name": texts.common_name if texts else plant["common_name"],
        "status": statuses,
        "is_due": any(status["state"] in ("due", "overdue") for status in statuses.values()),
        "winter_interval": care.watering_interval(plant_sheet, date(2000, 1, 15)) if plant_sheet else None,
        "summer_interval": care.watering_interval(plant_sheet, date(2000, 6, 15)) if plant_sheet else None,
    }


def _all_plant_views(lang: str) -> list[dict]:
    plants = [_plant_view(plant, lang) for plant in db.list_plants()]
    plants.sort(key=lambda plant: plant["name"].casefold())
    return plants


def _references(scientific_name: str, lang: str) -> list[tuple[str, str]]:
    t = TRANSLATIONS[lang]
    query = " ".join(scientific_name.split()[:2])  # genre + espèce : plus fiable que le cultivar
    if not query:
        return []
    return [
        (t["ref_wikipedia"], f"https://{lang}.wikipedia.org/w/index.php?search={quote_plus(query)}"),
        (t["ref_powo"], f"https://powo.science.kew.org/results?q={quote_plus(query)}"),
        (t["ref_aspca"], ASPCA_URL),
    ]


def _run_claude(call: Callable[[], BaseModel]) -> BaseModel | str:
    """Renvoie la réponse de Claude, ou un code d'erreur à afficher."""
    try:
        return call()
    except IdentificationError as error:
        return str(error)
    except anthropic.APIError:
        return "api_error"


def _identification_or_error(call: Callable[[], PlantIdentification]) -> PlantIdentification | str:
    result = _run_claude(call)
    if isinstance(result, str):
        return result
    if not result.is_plant or result.care is None or result.texts is None:
        return "not_a_plant"
    return result


def _get_plant_or_404(plant_id: int) -> dict:
    plant = db.get_plant(plant_id)
    if plant is None:
        raise HTTPException(status_code=404)
    return plant


def _read_upload(photo: UploadFile) -> tuple[bytes, str] | str:
    """Renvoie le contenu et l'extension de la photo, ou un code d'erreur."""
    extension = ALLOWED_TYPES.get(photo.content_type or "")
    if extension is None:
        return "unsupported_format"
    data = photo.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return "too_large"
    return data, extension


def _save_photo(data: bytes, extension: str) -> str:
    photo_file = f"{uuid4().hex}{extension}"
    (db.PHOTOS_DIR / photo_file).write_bytes(data)
    return photo_file


# ---------- PWA ----------

@app.get("/sw.js")
def service_worker():
    # Servi à la racine pour que le service worker contrôle toute l'appli
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


# ---------- Collection ----------

@app.get("/lang/{code}")
def set_language(code: str, next: str = "/"):
    response = RedirectResponse(_safe_next(next, "/"), status_code=303)
    if code in LANGUAGES:
        response.set_cookie("lang", code, max_age=ONE_YEAR, samesite="lax")
    return response


@app.get("/")
def index(request: Request, view: str = "grid", error: str | None = None):
    lang = get_language(request)
    plants = _all_plant_views(lang)
    views_by_id = {plant["id"]: plant for plant in plants}
    tasks = [
        {**task, "plant": views_by_id[task["plant"]["id"]],
         "label": views_by_id[task["plant"]["id"]]["status"][task["kind"]]["label"]}
        for task in care.due_tasks(db.list_plants())
    ]
    rooms: dict[str, list[dict]] = {}
    for plant in plants:
        rooms.setdefault(plant["location"].strip() or "", []).append(plant)
    return _render(request, "index.html", {
        "plants": plants,
        "tasks": tasks,
        "water_due": sum(1 for task in tasks if task["kind"] == "water"),
        "view": "rooms" if view == "rooms" else "grid",
        "rooms": sorted(rooms.items(), key=lambda item: (item[0] == "", item[0].casefold())),
        "error": error,
    })


@app.post("/plants")
def add_plant(photo: UploadFile = File(...), location: str = Form("")):
    upload = _read_upload(photo)
    if isinstance(upload, str):
        return _redirect_with_error("/", upload)
    data, extension = upload

    result = _identification_or_error(lambda: identify(data))
    if isinstance(result, str):
        return _redirect_with_error("/", result)

    plant_id = db.insert_plant(result, location.strip()[:120], _save_photo(data, extension))
    return RedirectResponse(f"/plants/{plant_id}", status_code=303)


@app.post("/care/water-all")
def water_all():
    for task in care.due_tasks(db.list_plants()):
        if task["kind"] == "water":
            db.add_event(task["plant"]["id"], "water", date.today())
    return RedirectResponse("/", status_code=303)


# ---------- Fiche ----------

def _diagnosis_view(photo: dict, lang: str) -> dict | None:
    if not photo["diagnosis"]:
        return None
    diagnosis = Diagnosis.model_validate(json.loads(photo["diagnosis"]))
    return {"severity": diagnosis.severity, "texts": getattr(diagnosis.texts, lang) if diagnosis.texts else None}


@app.get("/plants/{plant_id}")
def plant_detail(request: Request, plant_id: int, error: str | None = None):
    lang = get_language(request)
    plant = _get_plant_or_404(plant_id)
    events = {kind: db.list_events(plant_id, kind) for kind in db.CARE_KINDS}
    for kind, days in events.items():
        plant[f"last_{kind}"] = days[0] if days else None
    photos = [
        {**photo,
         "date_label": format_date(datetime.fromisoformat(photo["created_at"]).date(), lang),
         "is_main": photo["photo_file"] == plant["photo_file"],
         "diagnosis_view": _diagnosis_view(photo, lang)}
        for photo in db.list_photos(plant_id)
    ]
    return _render(request, "plant.html", {
        "plant": _plant_view(plant, lang),
        "history": {kind: [format_date(day, lang) for day in days] for kind, days in events.items()},
        "photos": photos,
        "diagnoses": [photo for photo in photos if photo["diagnosis_view"]],
        "references": _references(plant["scientific_name"], lang),
        "error": error,
    })


@app.post("/plants/{plant_id}/refresh")
def refresh_plant(plant_id: int):
    plant = _get_plant_or_404(plant_id)
    data = (db.PHOTOS_DIR / plant["photo_file"]).read_bytes()
    result = _identification_or_error(lambda: identify(data))
    if isinstance(result, str):
        return _redirect_with_error(f"/plants/{plant_id}", result)
    db.update_identification(plant_id, result)
    return RedirectResponse(f"/plants/{plant_id}", status_code=303)


@app.post("/plants/{plant_id}/rename")
def rename_plant(plant_id: int, name: str = Form("")):
    _get_plant_or_404(plant_id)
    name = name.strip()[:120]
    if not name:
        return _redirect_with_error(f"/plants/{plant_id}", "name_required")
    result = _identification_or_error(lambda: describe(name))
    if isinstance(result, str):
        return _redirect_with_error(f"/plants/{plant_id}", result)
    db.update_identification(plant_id, result)
    return RedirectResponse(f"/plants/{plant_id}", status_code=303)


@app.post("/plants/{plant_id}/location")
def update_location(plant_id: int, location: str = Form("")):
    _get_plant_or_404(plant_id)
    db.update_location(plant_id, location.strip()[:120])
    return RedirectResponse(f"/plants/{plant_id}", status_code=303)


@app.post("/plants/{plant_id}/care/{kind}")
def log_care(plant_id: int, kind: str, next: str = Form("")):
    _get_plant_or_404(plant_id)
    if kind not in db.CARE_KINDS:
        raise HTTPException(status_code=404)
    db.add_event(plant_id, kind, date.today())
    return RedirectResponse(_safe_next(next, f"/plants/{plant_id}"), status_code=303)


@app.post("/plants/{plant_id}/care/{kind}/undo")
def undo_care(plant_id: int, kind: str):
    _get_plant_or_404(plant_id)
    if kind not in db.CARE_KINDS:
        raise HTTPException(status_code=404)
    db.undo_last_event(plant_id, kind)
    return RedirectResponse(f"/plants/{plant_id}", status_code=303)


@app.post("/plants/{plant_id}/photos")
def add_growth_photo(plant_id: int, photo: UploadFile = File(...)):
    _get_plant_or_404(plant_id)
    upload = _read_upload(photo)
    if isinstance(upload, str):
        return _redirect_with_error(f"/plants/{plant_id}", upload)
    db.add_photo(plant_id, _save_photo(*upload), "growth")
    return RedirectResponse(f"/plants/{plant_id}#journal", status_code=303)


@app.post("/plants/{plant_id}/diagnose")
def diagnose_plant(request: Request, plant_id: int, photo: UploadFile = File(...)):
    plant = _get_plant_or_404(plant_id)
    plant_sheet = care.sheet(plant)
    if plant_sheet is None:
        return _redirect_with_error(f"/plants/{plant_id}", "outdated_sheet")
    upload = _read_upload(photo)
    if isinstance(upload, str):
        return _redirect_with_error(f"/plants/{plant_id}", upload)
    data, extension = upload

    texts = plant_sheet.texts.en
    result = _run_claude(lambda: diagnose(
        data,
        name=texts.common_name,
        scientific_name=plant["scientific_name"],
        watering=plant_sheet.care.watering_interval_days,
        light=texts.light,
        humidity=texts.humidity,
    ))
    if isinstance(result, str):
        return _redirect_with_error(f"/plants/{plant_id}", result)
    if not result.shows_plant or result.texts is None:
        return _redirect_with_error(f"/plants/{plant_id}", "not_a_plant")

    db.add_photo(plant_id, _save_photo(data, extension), "diagnosis", result)
    return RedirectResponse(f"/plants/{plant_id}#health", status_code=303)


@app.post("/journal/{photo_id}/main")
def set_main_photo(photo_id: int):
    photo = db.get_photo(photo_id)
    if photo is None:
        raise HTTPException(status_code=404)
    db.set_main_photo(photo["plant_id"], photo["photo_file"])
    return RedirectResponse(f"/plants/{photo['plant_id']}", status_code=303)


@app.post("/journal/{photo_id}/delete")
def delete_journal_photo(photo_id: int):
    photo = db.get_photo(photo_id)
    if photo is None:
        raise HTTPException(status_code=404)
    plant = _get_plant_or_404(photo["plant_id"])
    if photo["photo_file"] == plant["photo_file"]:
        return _redirect_with_error(f"/plants/{plant['id']}", "main_photo_delete")
    db.delete_photo(photo_id)
    (db.PHOTOS_DIR / photo["photo_file"]).unlink(missing_ok=True)
    return RedirectResponse(f"/plants/{plant['id']}#journal", status_code=303)


@app.post("/plants/{plant_id}/delete")
def delete_plant(plant_id: int):
    for photo_file in db.delete_plant(plant_id):
        (db.PHOTOS_DIR / photo_file).unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)


# ---------- Calendrier ----------

def _schedule(plants: list[dict], start: date, end: date, kinds=db.CARE_KINDS) -> dict[date, list[dict]]:
    schedule: dict[date, list[dict]] = {}
    for plant in plants:
        if plant["sheet"] is None:
            continue
        for kind in kinds:
            for day in care.occurrences(plant, plant["sheet"], kind, start, end):
                schedule.setdefault(day, []).append({
                    "plant": plant,
                    "kind": kind,
                    "overdue": plant["status"][kind]["state"] == "overdue" and day == start,
                })
    for entries in schedule.values():
        entries.sort(key=lambda entry: (db.CARE_KINDS.index(entry["kind"]), entry["plant"]["name"].casefold()))
    return schedule


@app.get("/calendar")
def calendar_page(request: Request):
    lang = get_language(request)
    t = TRANSLATIONS[lang]
    plants = _all_plant_views(lang)
    today = date.today()
    schedule = _schedule(plants, today, today + timedelta(days=CALENDAR_DAYS))
    start = today - timedelta(days=today.weekday())  # la grille commence un lundi
    days = []
    for offset in range((today - start).days + CALENDAR_DAYS):
        day = start + timedelta(days=offset)
        days.append({
            "date": day,
            "number": day.day,
            "label": format_date(day, lang),
            "is_today": day == today,
            "is_past": day < today,
            "entries": schedule.get(day, []),
        })
    feed_path = f"/calendar.ics?{urlencode({'token': _calendar_token(), 'lang': lang})}"
    return _render(request, "calendar.html", {
        "days": days,
        "weekdays": t["weekdays"],
        "untracked": [plant for plant in plants if plant["care"] and plant["status"]["water"]["state"] == "none"],
        "feed_url": str(request.base_url).rstrip("/") + feed_path,
        "notifications": {"enabled": notify.enabled(), "time": notify.NOTIFY_TIME},
    })


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@app.get("/calendar.ics")
def calendar_feed(request: Request, token: str = "", lang: str = "en"):
    if not hmac.compare_digest(token, _calendar_token()):
        raise HTTPException(status_code=404)
    lang = lang if lang in LANGUAGES else "en"
    t = TRANSLATIONS[lang]
    plants = _all_plant_views(lang)
    today = date.today()
    base_url = str(request.base_url).rstrip("/")
    stamp = today.strftime("%Y%m%dT000000Z")
    descriptions = {"water": "watering", "fertilize": "fertilizer", "repot": "pot"}
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//PlantCare//Care//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(t['calendar_name'])}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for day, entries in sorted(_schedule(plants, today, today + timedelta(days=ICS_DAYS)).items()):
        for entry in entries:
            plant, kind = entry["plant"], entry["kind"]
            summary = _ics_escape(t["care"][kind]["event"].format(name=plant["name"]))
            lines += [
                "BEGIN:VEVENT",
                f"UID:plantcare-{plant['id']}-{kind}-{day:%Y%m%d}",
                f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
                f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{_ics_escape(getattr(plant['texts'], descriptions[kind]))}",
                f"URL:{base_url}/plants/{plant['id']}",
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{summary}",
                "TRIGGER:PT9H",  # 9 h le jour même
                "END:VALARM",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8")


@app.post("/notifications/test")
def test_notification(request: Request):
    if not notify.enabled():
        return _redirect_with_error("/calendar", "notifications_disabled")
    t = TRANSLATIONS[get_language(request)]
    try:
        notify.send(t["notify_test_title"], t["notify_test_message"], ["seedling"])
    except OSError:
        return _redirect_with_error("/calendar", "notification_failed")
    return RedirectResponse("/calendar?notice=notification_sent", status_code=303)


# ---------- Mode vacances ----------

def _parse_range(start: str, end: str) -> tuple[date, date] | None:
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return None
    if last < first or (last - first).days > SITTER_MAX_DAYS:
        return None
    return first, last


@app.get("/vacation")
def vacation_page(request: Request, start: str = "", end: str = ""):
    today = date.today()
    period = _parse_range(start, end) if start and end else None
    share_url = None
    if period:
        query = urlencode({"start": period[0], "end": period[1], "token": _sitter_token(*period),
                           "lang": get_language(request)})
        share_url = f"{str(request.base_url).rstrip('/')}/sitter?{query}"
    return _render(request, "vacation.html", {
        "start": period[0].isoformat() if period else (today + timedelta(days=7)).isoformat(),
        "end": period[1].isoformat() if period else (today + timedelta(days=14)).isoformat(),
        "share_url": share_url,
        "error": "invalid_period" if (start or end) and not period else None,
    })


def _check_sitter_access(start: str, end: str, token: str) -> tuple[date, date]:
    period = _parse_range(start, end)
    if period is None or not hmac.compare_digest(token, _sitter_token(*period)):
        raise HTTPException(status_code=404)
    return period


@app.get("/sitter")
def sitter_page(request: Request, start: str = "", end: str = "", token: str = "", lang: str = ""):
    first, last = _check_sitter_access(start, end, token)
    lang = lang if lang in LANGUAGES else get_language(request)
    plants = [plant for plant in _all_plant_views(lang) if plant["sheet"]]
    # Pendant la garde, on suppose les soins faits à la date prévue : la projection reste valable
    schedule = _schedule(plants, first, last + timedelta(days=1), kinds=("water", "fertilize"))
    days = [
        {"label": format_date(day, lang), "weekday": TRANSLATIONS[lang]["weekdays"][day.weekday()],
         "entries": schedule[day]}
        for day in sorted(schedule)
    ]
    photo_query = urlencode({"start": start, "end": end, "token": token})
    # Le rendu suit la langue du lien, pas celle du navigateur de la personne qui garde les plantes
    return _render(request, "sitter.html", {
        "plants": plants,
        "days": days,
        "period": (format_date(first, lang), format_date(last, lang)),
        "photo_query": photo_query,
    }, lang=lang)


@app.get("/sitter/photos/{photo_file}")
def sitter_photo(photo_file: str, start: str = "", end: str = "", token: str = ""):
    _check_sitter_access(start, end, token)
    # Seulement les photos principales : le journal et les diagnostics restent privés
    if photo_file not in {plant["photo_file"] for plant in db.list_plants()}:
        raise HTTPException(status_code=404)
    return FileResponse(db.PHOTOS_DIR / photo_file)
