"""When is the agent allowed to call the AE? Pure functions, no I/O.

Rule: only on weekdays, between ``start_hour`` and ``end_hour`` in the AE's
own timezone (``app.account_executives.timezone``). A retry is never
earlier than ``now + delay``; if that lands outside the window it moves to
the start of the next allowed window.
"""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _zone(tz_name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def next_call_time(
    now_utc: datetime,
    tz_name: str | None,
    delay: timedelta,
    start_hour: int,
    end_hour: int,
) -> datetime:
    """Earliest allowed call time at or after ``now_utc + delay`` (returned in UTC)."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    zone = _zone(tz_name)
    candidate = (now_utc + delay).astimezone(zone)

    for _ in range(8):  # at most a week of skipping
        window_start = datetime.combine(candidate.date(), time(start_hour), tzinfo=zone)
        # An end hour of 24 means "until midnight" (``time(24)`` is not valid).
        window_end = datetime.combine(candidate.date(), time(end_hour % 24), tzinfo=zone) + timedelta(days=end_hour // 24)
        is_weekday = candidate.weekday() < 5

        if is_weekday and candidate < window_start:
            candidate = window_start
        if is_weekday and window_start <= candidate < window_end:
            return candidate.astimezone(timezone.utc)

        next_day = candidate.date() + timedelta(days=1)
        candidate = datetime.combine(next_day, time(start_hour), tzinfo=zone)

    return candidate.astimezone(timezone.utc)


def can_call_now(now_utc: datetime, tz_name: str | None, start_hour: int, end_hour: int) -> bool:
    return next_call_time(now_utc, tz_name, timedelta(0), start_hour, end_hour) <= now_utc
