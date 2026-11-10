"""Business-rule tests: the database, not the AI, is what keeps bookings safe."""

import threading
from datetime import timedelta

import pytest

from app import db, tools


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init()


def future_weekday(days_ahead: int = 7) -> str:
    """A date next week that is not a Sunday."""
    d = db.now() + timedelta(days=days_ahead)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d.date().isoformat()


def test_list_services():
    names = [s["name"] for s in tools.list_services()["services"]]
    assert "Classic haircut" in names and len(names) == 4


def test_sunday_is_closed():
    d = db.now()
    while d.weekday() != 6:
        d += timedelta(days=1)
    assert "error" in tools.check_availability(d.date().isoformat())


def test_booking_removes_the_slot():
    date = future_weekday()
    free_before = tools.check_availability(date)["free_slots"]
    assert "10:00" in free_before

    result = tools.book_appointment("Classic haircut", f"{date}T10:00", "Ana Ruiz", phone="111")
    assert result["booked"] and result["price_usd"] == 15.0

    free_after = tools.check_availability(date)["free_slots"]
    assert "10:00" not in free_after


def test_long_service_occupies_consecutive_slots():
    date = future_weekday()
    assert tools.book_appointment("Hair coloring", f"{date}T09:00", "Ana", phone="111")["booked"]
    free = tools.check_availability(date)["free_slots"]
    # 3 slots of 30 min: 9:00, 9:30 and 10:00 are gone
    assert {"09:00", "09:30", "10:00"}.isdisjoint(free) and "10:30" in free


def test_no_double_booking_even_with_20_concurrent_requests():
    date = future_weekday()
    results = []

    def try_book(i):
        r = tools.book_appointment("Classic haircut", f"{date}T15:00", f"Client {i}", phone=str(i))
        results.append("booked" in r)

    threads = [threading.Thread(target=try_book, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1  # exactly one winner, nineteen polite errors


def test_cannot_cancel_someone_elses_appointment():
    date = future_weekday()
    tools.book_appointment("Beard trim", f"{date}T11:00", "Ana", phone="111")
    appt_id = tools.my_appointments(phone="111")["appointments"][0]["id"]

    # Another customer (or a prompt-injected model) tries to cancel it
    assert "error" in tools.cancel_appointment(appt_id, phone="222")
    # The owner can
    assert tools.cancel_appointment(appt_id, phone="111")["cancelled"]
    assert tools.my_appointments(phone="111")["appointments"] == []


def test_rejects_past_and_out_of_hours():
    date = future_weekday()
    assert "error" in tools.book_appointment("Classic haircut", "2020-01-06T10:00", "Ana", phone="1")
    assert "error" in tools.book_appointment("Classic haircut", f"{date}T20:00", "Ana", phone="1")
    assert "error" in tools.book_appointment("Classic haircut", f"{date}T10:15", "Ana", phone="1")


def test_unknown_service_and_bad_dates_return_errors():
    assert "error" in tools.book_appointment("Nope", f"{future_weekday()}T10:00", "Ana", phone="1")
    assert "error" in tools.book_appointment("Classic haircut", "not-a-date", "Ana", phone="1")
    assert "error" in tools.check_availability("not-a-date")
