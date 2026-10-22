import json
import os
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from .models import Diagnosis, PlantIdentification

DATA_DIR = Path(os.environ.get("PLANTCARE_DATA", "data"))
PHOTOS_DIR = DATA_DIR / "photos"
DB_PATH = DATA_DIR / "plantcare.db"

CARE_KINDS = ("water", "fertilize", "repot")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    common_name TEXT NOT NULL,
    scientific_name TEXT NOT NULL,
    confidence REAL NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    photo_file TEXT NOT NULL,
    care TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS care_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    done_on TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS care_events_plant ON care_events (plant_id, kind, done_on);

CREATE TABLE IF NOT EXISTS plant_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
    photo_file TEXT NOT NULL,
    kind TEXT NOT NULL,
    diagnosis TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS plant_photos_plant ON plant_photos (plant_id, created_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Anciennes versions : une table dédiée aux arrosages, et pas de journal photo
MIGRATIONS = """
INSERT INTO plant_photos (plant_id, photo_file, kind, created_at)
SELECT id, photo_file, 'growth', created_at FROM plants
WHERE id NOT IN (SELECT plant_id FROM plant_photos);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_to_plant(row: sqlite3.Row) -> dict:
    plant = dict(row)
    plant["care"] = json.loads(plant["care"])
    return plant


def _sheet_json(identification: PlantIdentification) -> str:
    return json.dumps({
        "care": identification.care.model_dump(),
        "texts": identification.texts.model_dump(),
    }, ensure_ascii=False)


def init_storage() -> None:
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'waterings'").fetchone():
            conn.execute(
                "INSERT INTO care_events (plant_id, kind, done_on) SELECT plant_id, 'water', watered_on FROM waterings"
            )
            conn.execute("DROP TABLE waterings")
        conn.executescript(MIGRATIONS)


# ---------- Plantes ----------

def list_plants() -> list[dict]:
    last_by_kind = ", ".join(
        f"(SELECT MAX(done_on) FROM care_events WHERE plant_id = plants.id AND kind = '{kind}') AS last_{kind}"
        for kind in CARE_KINDS
    )
    with closing(_connect()) as conn:
        rows = conn.execute(f"SELECT plants.*, {last_by_kind} FROM plants").fetchall()
    return [_row_to_plant(row) for row in rows]


def get_plant(plant_id: int) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM plants WHERE id = ?", (plant_id,)).fetchone()
    return _row_to_plant(row) if row else None


def insert_plant(identification: PlantIdentification, location: str, photo_file: str) -> int:
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO plants (common_name, scientific_name, confidence, location, photo_file, care) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                identification.texts.en.common_name,
                identification.scientific_name,
                identification.confidence,
                location,
                photo_file,
                _sheet_json(identification),
            ),
        )
        conn.execute(
            "INSERT INTO plant_photos (plant_id, photo_file, kind) VALUES (?, ?, 'growth')",
            (cursor.lastrowid, photo_file),
        )
    return cursor.lastrowid


def update_identification(plant_id: int, identification: PlantIdentification) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE plants SET common_name = ?, scientific_name = ?, confidence = ?, care = ? WHERE id = ?",
            (
                identification.texts.en.common_name,
                identification.scientific_name,
                identification.confidence,
                _sheet_json(identification),
                plant_id,
            ),
        )


def update_location(plant_id: int, location: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE plants SET location = ? WHERE id = ?", (location, plant_id))


def delete_plant(plant_id: int) -> list[str]:
    """Supprime la plante et renvoie les fichiers photo à effacer."""
    with closing(_connect()) as conn, conn:
        row = conn.execute("SELECT photo_file FROM plants WHERE id = ?", (plant_id,)).fetchone()
        if row is None:
            return []
        files = {row["photo_file"]} | {
            photo["photo_file"]
            for photo in conn.execute("SELECT photo_file FROM plant_photos WHERE plant_id = ?", (plant_id,))
        }
        conn.execute("DELETE FROM plants WHERE id = ?", (plant_id,))
    return sorted(files)


# ---------- Soins ----------

def add_event(plant_id: int, kind: str, day: date) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO care_events (plant_id, kind, done_on) VALUES (?, ?, ?)",
            (plant_id, kind, day.isoformat()),
        )


def undo_last_event(plant_id: int, kind: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "DELETE FROM care_events WHERE id = (SELECT id FROM care_events WHERE plant_id = ? AND kind = ? "
            "ORDER BY done_on DESC, id DESC LIMIT 1)",
            (plant_id, kind),
        )


def list_events(plant_id: int, kind: str, limit: int = 6) -> list[date]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT done_on FROM care_events WHERE plant_id = ? AND kind = ? ORDER BY done_on DESC, id DESC LIMIT ?",
            (plant_id, kind, limit),
        ).fetchall()
    return [date.fromisoformat(row["done_on"]) for row in rows]


# ---------- Journal photo ----------

def add_photo(plant_id: int, photo_file: str, kind: str, diagnosis: Diagnosis | None = None) -> int:
    with closing(_connect()) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO plant_photos (plant_id, photo_file, kind, diagnosis) VALUES (?, ?, ?, ?)",
            (plant_id, photo_file, kind, diagnosis.model_dump_json() if diagnosis else None),
        )
    return cursor.lastrowid


def list_photos(plant_id: int) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM plant_photos WHERE plant_id = ? ORDER BY created_at DESC, id DESC", (plant_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def get_photo(photo_id: int) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM plant_photos WHERE id = ?", (photo_id,)).fetchone()
    return dict(row) if row else None


def delete_photo(photo_id: int) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM plant_photos WHERE id = ?", (photo_id,))


def set_main_photo(plant_id: int, photo_file: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("UPDATE plants SET photo_file = ? WHERE id = ?", (photo_file, plant_id))


# ---------- Réglages ----------

def get_setting(key: str) -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
