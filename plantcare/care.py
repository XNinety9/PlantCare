"""Planification des soins : saisons, prochaines échéances et tâches dues."""

import calendar
import os
from datetime import date, datetime, timedelta

from pydantic import ValidationError

from .db import CARE_KINDS
from .models import PlantSheet

# Saison de croissance : mars à septembre dans l'hémisphère nord, décalée de six mois au sud
HEMISPHERE = os.environ.get("PLANTCARE_HEMISPHERE", "north")
REPOT_FALLBACK_MONTHS = 24


def sheet(plant: dict) -> PlantSheet | None:
    care = plant["care"].get("care")
    if isinstance(care, dict):
        # Fiches générées avant l'ajout des saisons, de l'engrais et du rempotage
        care.setdefault("watering_interval_winter_days", care.get("watering_interval_days"))
        care.setdefault("fertilize_interval_days", 0)
        care.setdefault("repot_interval_months", REPOT_FALLBACK_MONTHS)
    try:
        return PlantSheet.model_validate(plant["care"])
    except ValidationError:
        return None  # fiche à l'ancien format, à régénérer


def is_growing_season(day: date) -> bool:
    month = day.month if HEMISPHERE == "north" else (day.month + 5) % 12 + 1
    return 3 <= month <= 9


def _next_growing_day(day: date) -> date:
    while not is_growing_season(day):
        day = (day.replace(day=1) + timedelta(days=32)).replace(day=1)
    return day


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def watering_interval(plant_sheet: PlantSheet, day: date) -> int:
    care = plant_sheet.care
    interval = care.watering_interval_days if is_growing_season(day) else care.watering_interval_winter_days
    return max(interval, 1)


def created_on(plant: dict) -> date:
    return datetime.fromisoformat(plant["created_at"]).date()


def last_done(plant: dict, kind: str) -> date | None:
    value = plant.get(f"last_{kind}")
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def next_due(plant: dict, plant_sheet: PlantSheet, kind: str) -> date | None:
    """Prochaine échéance, éventuellement dans le passé si le soin est en retard."""
    last = last_done(plant, kind)
    if kind == "water":
        # L'arrosage ne se planifie qu'à partir d'un premier arrosage noté
        return last + timedelta(days=watering_interval(plant_sheet, last)) if last else None
    if kind == "fertilize":
        interval = plant_sheet.care.fertilize_interval_days
        if interval <= 0:
            return None
        return _next_growing_day((last or created_on(plant)) + timedelta(days=interval))
    if kind == "repot":
        months = plant_sheet.care.repot_interval_months or REPOT_FALLBACK_MONTHS
        return _add_months(last or created_on(plant), months)
    raise ValueError(kind)


def occurrences(plant: dict, plant_sheet: PlantSheet, kind: str, start: date, end: date) -> list[date]:
    """Échéances entre start (inclus) et end (exclu). Un soin en retard tombe sur start."""
    due = next_due(plant, plant_sheet, kind)
    if due is None:
        return []
    day = max(due, start)
    result = []
    while day < end:
        result.append(day)
        if kind == "water":
            day += timedelta(days=watering_interval(plant_sheet, day))
        elif kind == "fertilize":
            day = _next_growing_day(day + timedelta(days=plant_sheet.care.fertilize_interval_days))
        else:
            break  # un rempotage tous les un ou deux ans : une seule occurrence suffit
    return result


def due_tasks(plants: list[dict], today: date | None = None) -> list[dict]:
    """Soins à faire aujourd'hui ou en retard, les plus en retard d'abord."""
    today = today or date.today()
    tasks = []
    for plant in plants:
        plant_sheet = sheet(plant)
        if plant_sheet is None:
            continue
        for kind in CARE_KINDS:
            due = next_due(plant, plant_sheet, kind)
            if due is not None and due <= today:
                tasks.append({"plant": plant, "kind": kind, "due": due, "days_late": (today - due).days})
    tasks.sort(key=lambda task: (-task["days_late"], CARE_KINDS.index(task["kind"])))
    return tasks
