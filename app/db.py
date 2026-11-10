"""SQLite storage for the demo business (a barbershop).

The one rule that matters: a time slot can only be booked once, and the
database enforces it — not the AI, not the Python code. A partial unique
index on active appointments makes double-booking impossible even if two
clients book the same slot at the exact same moment.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "barber.db"

OPEN_HOUR, CLOSE_HOUR = 9, 19  # Mon-Sat
SLOT_MINUTES = 30

SERVICES = [
    ("Classic haircut", 15.0, 1),        # 1 slot = 30 min
    ("Haircut + beard", 22.0, 2),
    ("Beard trim", 10.0, 1),
    ("Hair coloring", 40.0, 3),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    price_usd REAL NOT NULL,
    slots INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY,
    customer TEXT NOT NULL,
    phone TEXT NOT NULL,
    service_id INTEGER NOT NULL REFERENCES services(id),
    start TEXT NOT NULL,            -- ISO datetime of the first slot
    slot TEXT NOT NULL,             -- one row per occupied slot
    status TEXT NOT NULL DEFAULT 'confirmed'
);
-- The lock against double-booking: only one ACTIVE appointment per slot.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_slot
    ON appointments(slot) WHERE status = 'confirmed';
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0:
            conn.executemany(
                "INSERT INTO services(name, price_usd, slots) VALUES (?, ?, ?)", SERVICES
            )


def now() -> datetime:
    return datetime.now()


def day_slots(date: str) -> list[datetime]:
    """Every slot the shop offers on a date (no Sundays)."""
    day = datetime.fromisoformat(date)
    if day.weekday() == 6:
        return []
    slots, t = [], day.replace(hour=OPEN_HOUR, minute=0)
    end = day.replace(hour=CLOSE_HOUR, minute=0)
    while t < end:
        slots.append(t)
        t += timedelta(minutes=SLOT_MINUTES)
    return slots


def slot_range(start: datetime, n: int) -> list[str]:
    return [(start + timedelta(minutes=SLOT_MINUTES * i)).isoformat() for i in range(n)]
