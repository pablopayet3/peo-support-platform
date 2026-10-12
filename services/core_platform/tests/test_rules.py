from datetime import date

from app.rules import enrollment_window


def test_deadline_is_1159pm_local_on_the_last_day():
    # Born October 6, 30-day window: closes November 5 at 11:59 pm Eastern (EST after Nov 1) = 04:59 UTC Nov 6
    w = enrollment_window(date(2026, 10, 6), 30, "America/New_York", today=date(2026, 10, 12))
    assert w.last_day == date(2026, 11, 5)
    assert w.deadline_utc.isoformat() == "2026-11-06T04:59:00+00:00"
    assert w.is_open


def test_window_closed_after_last_day():
    w = enrollment_window(date(2026, 9, 1), 30, "America/New_York", today=date(2026, 10, 12))
    assert not w.is_open


def test_last_day_itself_is_open():
    w = enrollment_window(date(2026, 9, 12), 30, "America/Los_Angeles", today=date(2026, 10, 12))
    assert w.last_day == date(2026, 10, 12) and w.is_open
