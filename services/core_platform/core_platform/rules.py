"""Business rules for qualifying life events (QLE). Pure functions, so they are easy to test."""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import get_settings

DEFAULT_WINDOWS = {"newborn": 30, "adoption": 30, "marriage": 30, "loss_of_coverage": 30}


def today_in(tz: str) -> date:
    frozen = get_settings().frozen_today
    if frozen:
        return date.fromisoformat(frozen)
    return datetime.now(ZoneInfo(tz)).date()


@dataclass(frozen=True)
class Window:
    last_day: date
    deadline_utc: datetime
    is_open: bool


def enrollment_window(event_date: date, window_days: int, tz: str, today: date | None = None) -> Window:
    """The window closes at 11:59 pm on the last day, in the company's time zone.
    Example: a birth on October 6 with a 30-day window closes on November 5 at 11:59 pm."""
    last_day = event_date + timedelta(days=window_days)
    local_deadline = datetime.combine(last_day, time(23, 59), tzinfo=ZoneInfo(tz))
    today = today or today_in(tz)
    return Window(
        last_day=last_day,
        deadline_utc=local_deadline.astimezone(timezone.utc),
        is_open=event_date <= today <= last_day,
    )
