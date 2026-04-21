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
"""


ITINERARY_SEED = [
    (1, "2026-07-25", "En vuelo", "Salida EZE noche", 0),
    (2, "2026-07-26", "Escala NYC", "Escala JFK", 13),
    (3, "2026-07-27", "Barcelona", "Llegada BCN, reencuentro", 5),
    (4, "2026-07-28", "Barcelona", "Día libre + vuelo nocturno Roma 21:45", 5),
    (5, "2026-07-29", "Roma", "Llegada 01:45, Audiencia Papal 10h", 15),
    (6, "2026-07-30", "Roma", "Coliseo + Foro Romano", 35),
    (7, "2026-07-31", "Roma", "Trevi + Pantheon + vuelo 18:10 BCN", 15),
    (8, "2026-08-01", "Barcelona", "Llegada 20h, descanso", 5),
    (9, "2026-08-02", "Barcelona", "Playa Barceloneta", 2),
    (10, "2026-08-03", "Madrid", "Tren BCN→MAD, llegada", 40),
    (11, "2026-08-04", "Madrid", "Museo Legends + Tour Bernabéu", 76),
    (12, "2026-08-05", "Madrid", "Retiro + Reina Sofía gratis", 20),
    (13, "2026-08-06", "Barcelona", "El Rastro + tren MAD→BCN", 40),
    (14, "2026-08-07", "Barcelona", "Descanso post-Madrid", 2),
    (15, "2026-08-08", "Barcelona", "Bunkers del Carmel", 2),
    (16, "2026-08-09", "Barcelona", "Escapada Montserrat", 33),
    (17, "2026-08-10", "Barcelona", "Sagrada Família exterior", 2),
    (18, "2026-08-11", "Barcelona", "Poblenou", 3),
    (19, "2026-08-12", "Barcelona", "Escapada Sitges", 13),
    (20, "2026-08-13", "Barcelona", "Park Güell", 2),
    (21, "2026-08-14", "Barcelona", "Último día libre", 10),
    (22, "2026-08-15", "Barcelona", "Vuelo regreso EZE", 5),
]


# (concept, detail, amount_eur, status, reservation_code)
# Todos los items arrancan como "pending" y sin monto cargado: el usuario
# manda los comprobantes al bot y la IA completa los precios reales.
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
    ("Audiencia Papal Vaticano", "29-jul · GRATIS · pendiente reserva", 0, "pending", None),
    ("Coliseo + Foro Romano", "30-jul", 18, "pending", None),
    ("Tour Bernabéu", "4-ago", 35, "pending", None),
    ("Museo Legends Madrid", "4-ago", 28, "pending", None),
    ("Montserrat FGC + cremallera", "9-ago", 25, "pending", None),
    ("T-Jove Barcelona", "Transporte en Barcelona", 40, "pending", None),
    ("Bono metro Madrid", "Transporte en Madrid", 12, "pending", None),
    ("eSIM Europa", "Datos móviles", 15, "pending", None),
]


CONFIG_SEED = [
    ("eur_usd_rate", os.getenv("EUR_USD_RATE", "1.172")),
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
