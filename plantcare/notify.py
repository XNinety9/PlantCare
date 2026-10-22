"""Résumé quotidien des soins, envoyé par ntfy (https://ntfy.sh ou une instance auto-hébergée)."""

import asyncio
import json
import logging
import os
import urllib.request
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from . import care, db
from .i18n import TRANSLATIONS, plural

NTFY_URL = os.environ.get("PLANTCARE_NTFY_URL", "")  # ex. https://ntfy.sh/mes-plantes-a1b2c3
NTFY_TOKEN = os.environ.get("PLANTCARE_NTFY_TOKEN", "")
NOTIFY_TIME = os.environ.get("PLANTCARE_NOTIFY_TIME", "08:30")
NOTIFY_LANG = os.environ.get("PLANTCARE_NOTIFY_LANG", "en")
PUBLIC_URL = os.environ.get("PLANTCARE_PUBLIC_URL", "").rstrip("/")

LAST_SENT_KEY = "notification_last_sent"
KIND_TAGS = {"water": "droplet", "fertilize": "seedling", "repot": "potted_plant"}

logger = logging.getLogger("plantcare.notify")


def enabled() -> bool:
    return bool(NTFY_URL)


def send(title: str, message: str, tags: list[str] | None = None) -> None:
    # Publication JSON : contrairement aux en-têtes HTTP, elle accepte les accents dans le titre
    parts = urlsplit(NTFY_URL)
    payload = {"topic": parts.path.strip("/"), "title": title, "message": message, "tags": tags or []}
    if PUBLIC_URL:
        payload["click"] = PUBLIC_URL
    request = urllib.request.Request(
        f"{parts.scheme}://{parts.netloc}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if NTFY_TOKEN:
        request.add_header("Authorization", f"Bearer {NTFY_TOKEN}")
    with urllib.request.urlopen(request, timeout=15):
        pass


def daily_summary(lang: str = NOTIFY_LANG) -> tuple[str, str, list[str]] | None:
    t = TRANSLATIONS[lang]
    tasks = care.due_tasks(db.list_plants())
    if not tasks:
        return None
    lines = []
    for task in tasks:
        plant_sheet = care.sheet(task["plant"])
        name = getattr(plant_sheet.texts, lang).common_name
        line = t["care"][task["kind"]]["event"].format(name=name)
        if task["days_late"]:
            line += f" ({plural(t['care'][task['kind']]['overdue'], task['days_late'])})"
        lines.append(line)
    title = plural(t["notify_title"], len(tasks))
    tags = sorted({KIND_TAGS[task["kind"]] for task in tasks})
    return title, "\n".join(lines), tags


def send_daily_summary() -> bool:
    """Envoie le résumé du jour, au plus une fois par jour. Renvoie True si un message est parti."""
    today = date.today().isoformat()
    if db.get_setting(LAST_SENT_KEY) == today:
        return False
    summary = daily_summary()
    db.set_setting(LAST_SENT_KEY, today)
    if summary is None:
        return False
    send(*summary)
    return True


def _todays_run(now: datetime) -> datetime:
    hour, minute = (int(part) for part in NOTIFY_TIME.split(":"))
    return datetime.combine(now.date(), time(hour, minute))


def _next_run(now: datetime) -> datetime:
    run = _todays_run(now)
    return run if run > now else run + timedelta(days=1)


async def scheduler() -> None:
    # Au démarrage, rattrape le résumé du jour si l'heure est passée et qu'il n'a pas été envoyé
    if datetime.now() >= _todays_run(datetime.now()):
        await _safe_send()
    while True:
        await asyncio.sleep((_next_run(datetime.now()) - datetime.now()).total_seconds())
        await _safe_send()


async def _safe_send() -> None:
    try:
        await asyncio.to_thread(send_daily_summary)
    except Exception:
        logger.exception("Envoi de la notification quotidienne impossible")
