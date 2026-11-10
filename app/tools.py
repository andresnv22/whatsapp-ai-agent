"""The tools the AI agent is allowed to use.

Business rules live HERE, in code — never in the prompt. The model can only
see availability, book and cancel through these functions, so it cannot
invent prices, book outside opening hours or touch another customer's data
(the customer's phone number is injected by the system, not by the model).
"""

import sqlite3
from datetime import datetime

from . import db

TOOLS = [
    {
        "name": "list_services",
        "description": "List the services the barbershop offers, with prices and duration.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_availability",
        "description": "Free time slots for a given date (YYYY-MM-DD).",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["date"],
        },
    },
    {
        "name": "book_appointment",
        "description": "Book an appointment. Needs service name, date+time and customer name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM"},
                "customer": {"type": "string", "description": "Customer's full name"},
            },
            "required": ["service", "start", "customer"],
        },
    },
    {
        "name": "my_appointments",
        "description": "The customer's upcoming appointments.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel one of the customer's appointments by its id.",
        "input_schema": {
            "type": "object",
            "properties": {"appointment_id": {"type": "integer"}},
            "required": ["appointment_id"],
        },
    },
]

# These tools receive the customer's phone from the system (never from the model).
NEED_PHONE = {"book_appointment", "my_appointments", "cancel_appointment"}


def list_services() -> dict:
    with db.connect() as conn:
        rows = conn.execute("SELECT name, price_usd, slots FROM services").fetchall()
    return {"services": [
        {"name": r["name"], "price_usd": r["price_usd"], "minutes": r["slots"] * db.SLOT_MINUTES}
        for r in rows
    ]}


def check_availability(date: str) -> dict:
    try:
        slots = db.day_slots(date)
    except ValueError:
        return {"error": "Invalid date. Use YYYY-MM-DD."}
    if not slots:
        return {"error": "The shop is closed on Sundays."}
    with db.connect() as conn:
        taken = {r["slot"] for r in conn.execute(
            "SELECT slot FROM appointments WHERE status = 'confirmed' AND slot LIKE ?",
            (f"{date}%",),
        )}
    free = [s.strftime("%H:%M") for s in slots if s.isoformat() not in taken and s > db.now()]
    return {"date": date, "free_slots": free}


def book_appointment(service: str, start: str, customer: str, phone: str) -> dict:
    with db.connect() as conn:
        svc = conn.execute(
            "SELECT * FROM services WHERE LOWER(name) = LOWER(?)", (service.strip(),)
        ).fetchone()
    if svc is None:
        return {"error": f"Unknown service '{service}'. Use list_services first."}
    try:
        begin = datetime.fromisoformat(start)
    except ValueError:
        return {"error": "Invalid start. Use YYYY-MM-DDTHH:MM."}
    if begin <= db.now():
        return {"error": "That time is in the past."}
    day = db.day_slots(begin.date().isoformat())
    needed = db.slot_range(begin, svc["slots"])
    if not day or begin not in day or needed[-1] > day[-1].isoformat():
        return {"error": "Outside opening hours (Mon-Sat, 9:00-19:00, every 30 min)."}

    try:
        # One transaction for all the slots the service needs. If any slot is
        # taken, the unique index aborts the whole booking — no partial holds,
        # no double-booking, even under concurrent requests.
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "INSERT INTO appointments(customer, phone, service_id, start, slot) VALUES (?, ?, ?, ?, ?)",
                [(customer.strip(), phone, svc["id"], begin.isoformat(), s) for s in needed],
            )
    except sqlite3.IntegrityError:
        return {"error": "That slot was just taken. Check availability again."}

    return {"booked": True, "service": svc["name"], "start": begin.isoformat(),
            "price_usd": svc["price_usd"], "customer": customer.strip()}


def my_appointments(phone: str) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT MIN(a.id) AS id, a.customer, s.name AS service, a.start
               FROM appointments a JOIN services s ON s.id = a.service_id
               WHERE a.phone = ? AND a.status = 'confirmed' AND a.start >= ?
               GROUP BY a.start, a.service_id ORDER BY a.start""",
            (phone, db.now().isoformat()),
        ).fetchall()
    return {"appointments": [dict(r) for r in rows]}


def cancel_appointment(appointment_id: int, phone: str) -> dict:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT start, service_id, phone FROM appointments WHERE id = ?", (appointment_id,)
        ).fetchone()
        # Only the owner of the appointment can cancel it, whatever the model asks for.
        if row is None or row["phone"] != phone:
            return {"error": "No such appointment for this customer."}
        conn.execute(
            "UPDATE appointments SET status = 'cancelled' WHERE start = ? AND service_id = ? AND phone = ?",
            (row["start"], row["service_id"], phone),
        )
    return {"cancelled": True}


FUNCTIONS = {
    "list_services": list_services,
    "check_availability": check_availability,
    "book_appointment": book_appointment,
    "my_appointments": my_appointments,
    "cancel_appointment": cancel_appointment,
}
