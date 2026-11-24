"""SQLite storage for the demo business (a barbershop).

The one rule that matters: a time slot can only be booked once, and the
database enforces it — not the AI, not the Python code. A partial unique
index on active appointments makes double-booking impossible even if two
clients book the same slot at the exact same moment.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DB_PATH = Path(__file__).resolve().parent.parent / "barber.db"

OPEN_HOUR, CLOSE_HOUR = 9, 19  # Mon-Sat
SLOT_MINUTES = 30
DEFAULT_TIMEZONE = "America/Bogota"

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

CREATE TABLE IF NOT EXISTS conversations (
    phone TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);
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


def shop_timezone() -> ZoneInfo:
    name = os.environ.get("SHOP_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def now() -> datetime:
    """Current wall-clock time in the shop's timezone (naive, for SQLite ISO comparison)."""
    return datetime.now(shop_timezone()).replace(tzinfo=None)


def claim_message(message_id: str, ttl: float) -> bool:
    """Record a WhatsApp message id atomically. Returns True if already seen within ttl."""
    if not message_id:
        return False
    ts = time.time()
    cutoff = ts - ttl
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM processed_messages WHERE seen_at < ?", (cutoff,))
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_messages(message_id, seen_at) VALUES (?, ?)",
            (message_id, ts),
        )
        return cur.rowcount == 0


def load_conversation(phone: str, provider: str, idle_reset: float) -> list[dict]:
    """Load persisted chat history for phone if fresh and matching provider."""
    with connect() as conn:
        row = conn.execute(
            "SELECT provider, messages_json, updated_at FROM conversations WHERE phone = ?",
            (phone,),
        ).fetchone()
    if row is None or row["provider"] != provider or time.time() - row["updated_at"] > idle_reset:
        return []
    try:
        data = json.loads(row["messages_json"])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def save_conversation(phone: str, provider: str, messages: list[dict]) -> None:
    payload = json.dumps(messages, ensure_ascii=False)
    with connect() as conn:
        conn.execute(
            """INSERT INTO conversations(phone, provider, messages_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET
                   provider = excluded.provider,
                   messages_json = excluded.messages_json,
                   updated_at = excluded.updated_at""",
            (phone, provider, payload, time.time()),
        )


def delete_conversation(phone: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM conversations WHERE phone = ?", (phone,))


def list_appointments() -> list[dict]:
    """Return grouped appointments for the live simulator/admin view."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT MIN(a.id) AS id, a.customer, a.phone, s.name AS service, a.start, a.status
               FROM appointments a JOIN services s ON s.id = a.service_id
               GROUP BY a.start, a.service_id, a.phone, a.status
               ORDER BY a.start DESC, id DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


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
