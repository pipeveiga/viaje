"""SQLite database layer for TripDesk.

Uses sqlite3 directly (no ORM) to keep the project simple and portable.
Exposes a `get_db()` helper and a `init_db()` function that seeds the
itinerary and checklist with the fixed trip data.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = os.getenv("DATABASE_PATH", "tripdesk.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS itinerary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_number INTEGER UNIQUE NOT NULL,
    date TEXT NOT NULL,
    city TEXT NOT NULL,
    activity TEXT NOT NULL,
    estimated_expense REAL NOT NULL DEFAULT 0,
    real_expense REAL,
    status TEXT NOT NULL DEFAULT 'empty',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS checklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept TEXT NOT NULL,
    detail TEXT,
    amount_eur REAL,
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    paid_date TEXT,
    reservation_code TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_path TEXT NOT NULL,
    doc_type TEXT,
    description TEXT,
    amount_eur REAL,
    amount_usd REAL,
    date TEXT,
    provider TEXT,
    reservation_number TEXT,
    day_number INTEGER,
    checklist_id INTEGER,
    confidence TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    data_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_conversation_chat ON conversation_turns(chat_id, id);
"""


# Base de €40/día para comida y cosas del día a día. Las actividades,
# museos y excursiones las va cargando Felipe vía el bot o el dashboard.
DAILY_FOOD_BASE = 40.0

ITINERARY_SEED = [
    (1, "2026-07-25", "En vuelo", "", DAILY_FOOD_BASE),
    (2, "2026-07-26", "Escala NYC", "", DAILY_FOOD_BASE),
    (3, "2026-07-27", "Barcelona", "", DAILY_FOOD_BASE),
    (4, "2026-07-28", "Barcelona", "", DAILY_FOOD_BASE),
    (5, "2026-07-29", "Roma", "", DAILY_FOOD_BASE),
    (6, "2026-07-30", "Roma", "", DAILY_FOOD_BASE),
    (7, "2026-07-31", "Roma", "", DAILY_FOOD_BASE),
    (8, "2026-08-01", "Barcelona", "", DAILY_FOOD_BASE),
    (9, "2026-08-02", "Barcelona", "", DAILY_FOOD_BASE),
    (10, "2026-08-03", "Madrid", "", DAILY_FOOD_BASE),
    (11, "2026-08-04", "Madrid", "", DAILY_FOOD_BASE),
    (12, "2026-08-05", "Madrid", "", DAILY_FOOD_BASE),
    (13, "2026-08-06", "Barcelona", "", DAILY_FOOD_BASE),
    (14, "2026-08-07", "Barcelona", "", DAILY_FOOD_BASE),
    (15, "2026-08-08", "Barcelona", "", DAILY_FOOD_BASE),
    (16, "2026-08-09", "Barcelona", "", DAILY_FOOD_BASE),
    (17, "2026-08-10", "Barcelona", "", DAILY_FOOD_BASE),
    (18, "2026-08-11", "Barcelona", "", DAILY_FOOD_BASE),
    (19, "2026-08-12", "Barcelona", "", DAILY_FOOD_BASE),
    (20, "2026-08-13", "Barcelona", "", DAILY_FOOD_BASE),
    (21, "2026-08-14", "Barcelona", "", DAILY_FOOD_BASE),
    (22, "2026-08-15", "Barcelona", "", DAILY_FOOD_BASE),
]


# (concept, detail, amount_eur, status, reservation_code)
# Sólo pagos anticipados obligatorios del viaje (vuelos, hoteles, trenes,
# buses, transporte local prepagado, datos). Los museos/actividades NO
# entran acá: Felipe decide on-the-fly si va y, si va, los carga como
# actividad del día desde el bot.
CHECKLIST_SEED = [
    ("Vuelo EZE→BCN ida", "Vuelo internacional Buenos Aires → Barcelona", None, "pending", None),
    ("Vuelo BCN→FCO", "Wizz Air W4 6020, 28-jul 21:45", None, "pending", None),
    ("Vuelo FCO→BCN", "Ryanair FR 6342, 31-jul 18:10", None, "pending", "H5RT7X"),
    ("Vuelo BCN→EZE vuelta", "Vuelo internacional Barcelona → Buenos Aires", None, "pending", None),
    ("Tren BCN→MAD", "Omio, 3-ago", None, "pending", "RFU685/93VZXL"),
    ("Tren MAD→BCN", "Omio, 6-ago", None, "pending", "RFU685/93VZXL"),
    ("Bus FCO→Roma Vaticano", "29-jul 01:15", None, "pending", None),
    ("Bus Roma Vaticano→FCO", "31-jul 15:00", None, "pending", None),
    ("Hotel Roma Grand Hotel Olympic", "29-31 jul", None, "pending", None),
    ("Hotel Madrid Colectia Stays Atocha", "3-6 ago", None, "pending", None),
    ("T-Jove Barcelona", "Transporte en Barcelona", None, "pending", None),
    ("Bono metro Madrid", "Transporte en Madrid", None, "pending", None),
    ("eSIM Europa", "Datos móviles", None, "pending", None),
]


# Items que antes estaban en el checklist y ahora no queremos más (los va
# cargando Felipe como actividades del día si decide ir). Se borran en cada
# init_db para limpiar DBs existentes.
CHECKLIST_LEGACY_CONCEPTS_TO_REMOVE = [
    "Audiencia Papal Vaticano",
    "Coliseo + Foro Romano",
    "Tour Bernabéu",
    "Museo Legends Madrid",
    "Montserrat FGC + cremallera",
]


CONFIG_SEED = [
    ("eur_usd_rate", os.getenv("EUR_USD_RATE", "1.172")),
    ("usd_ars_rate", os.getenv("USD_ARS_RATE", "1450")),
    ("trip_name", "Europa 2026"),
    ("traveler", "Felipe Veiga"),
    ("start_date", "2026-07-25"),
    ("end_date", "2026-08-15"),
]


def init_db() -> None:
    """Create tables and seed initial data if the DB is empty."""
    Path(os.getenv("UPLOADS_PATH", "uploads")).mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA)

        cur = conn.execute("SELECT COUNT(*) AS c FROM itinerary")
        if cur.fetchone()["c"] == 0:
            conn.executemany(
                """INSERT INTO itinerary
                   (day_number, date, city, activity, estimated_expense)
                   VALUES (?, ?, ?, ?, ?)""",
                ITINERARY_SEED,
            )

        cur = conn.execute("SELECT COUNT(*) AS c FROM checklist")
        if cur.fetchone()["c"] == 0:
            now = "2026-07-01"
            rows = []
            for concept, detail, amount, status, code in CHECKLIST_SEED:
                paid_date = now if status == "paid" else None
                rows.append((concept, detail, amount, None, status, paid_date, code))
            conn.executemany(
                """INSERT INTO checklist
                   (concept, detail, amount_eur, deadline, status, paid_date, reservation_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        else:
            # Migración idempotente: borrar items que ahora no queremos (los
            # museos/actividades pasaron a ser cosas que Felipe carga como
            # actividad del día sólo si decide ir).
            for legacy in CHECKLIST_LEGACY_CONCEPTS_TO_REMOVE:
                conn.execute(
                    "DELETE FROM checklist WHERE concept = ?",
                    (legacy,),
                )

        for key, value in CONFIG_SEED:
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )


def get_config(key: str, default: str | None = None) -> str | None:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            """INSERT INTO config (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )


def reset_paid_items() -> int:
    """Mark every paid checklist item as pending and clear its amount.

    Returns the number of rows affected.
    """
    with get_db() as conn:
        cur = conn.execute(
            """UPDATE checklist
               SET status = 'pending', paid_date = NULL, amount_eur = NULL
               WHERE status = 'paid'"""
        )
        return cur.rowcount


def reset_all_data() -> dict:
    """Wipe documents + pending confirmations, clear all checklist amounts and
    itinerary activities/real expenses. Keep the structure (days, cities,
    checklist items) so the bot can re-fill everything from scratch.

    Returns a summary with row counts.
    """
    uploads_dir = Path(os.getenv("UPLOADS_PATH", "uploads"))
    removed_files = 0
    with get_db() as conn:
        paths = [
            row["file_path"]
            for row in conn.execute("SELECT file_path FROM documents").fetchall()
        ]
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
                removed_files += 1
            except OSError:
                pass

        docs_cur = conn.execute("DELETE FROM documents")
        pend_cur = conn.execute("DELETE FROM pending_confirmations")
        ch_cur = conn.execute(
            """UPDATE checklist
               SET status = 'pending',
                   paid_date = NULL,
                   amount_eur = NULL"""
        )
        it_cur = conn.execute(
            """UPDATE itinerary
               SET activity = '',
                   estimated_expense = 40,
                   real_expense = NULL,
                   status = 'empty',
                   notes = NULL"""
        )

    # If the uploads dir is now empty, leave it be (it's fine).
    if uploads_dir.exists():
        try:
            for stray in uploads_dir.iterdir():
                if stray.is_file():
                    stray.unlink(missing_ok=True)
                    removed_files += 1
        except OSError:
            pass

    return {
        "documents_deleted": docs_cur.rowcount,
        "pending_cleared": pend_cur.rowcount,
        "checklist_reset": ch_cur.rowcount,
        "itinerary_reset": it_cur.rowcount,
        "files_removed": removed_files,
    }
