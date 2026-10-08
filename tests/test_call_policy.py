from datetime import datetime, timedelta, timezone

from src.agents.tier_confirmation.call_policy import can_call_now, next_call_time

UTC = timezone.utc
# 2026-10-05 is a Monday
MON_10_UTC = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


def test_inside_window_calls_now():
    assert next_call_time(MON_10_UTC, "UTC", timedelta(0), 9, 19) == MON_10_UTC
    assert can_call_now(MON_10_UTC, "UTC", 9, 19)


def test_an_end_hour_of_24_means_until_midnight():
    late_evening = datetime(2026, 10, 5, 23, 30, tzinfo=UTC)
    assert next_call_time(late_evening, "UTC", timedelta(0), 0, 24) == late_evening
    assert can_call_now(datetime(2026, 10, 5, 2, 0, tzinfo=UTC), "UTC", 0, 24)


def test_retry_delay_is_applied():
    assert next_call_time(MON_10_UTC, "UTC", timedelta(minutes=30), 9, 19) == MON_10_UTC + timedelta(minutes=30)


def test_before_window_moves_to_window_start():
    early = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
    assert next_call_time(early, "UTC", timedelta(0), 9, 19) == datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def test_after_window_moves_to_next_morning():
    late = datetime(2026, 10, 5, 18, 50, tzinfo=UTC)
    # +30 min lands at 19:20, past the window → Tuesday 09:00
    assert next_call_time(late, "UTC", timedelta(minutes=30), 9, 19) == datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def test_friday_evening_skips_weekend():
    fri_late = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    assert next_call_time(fri_late, "UTC", timedelta(0), 9, 19) == datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def test_ae_timezone_is_respected():
    # 10:00 UTC = 03:00 in Los Angeles → wait until 09:00 PDT = 16:00 UTC
    result = next_call_time(MON_10_UTC, "America/Los_Angeles", timedelta(0), 9, 19)
    assert result == datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
    # 10:00 UTC = 15:30 IST → inside the window
    assert can_call_now(MON_10_UTC, "Asia/Kolkata", 9, 19)


def test_unknown_timezone_falls_back_to_utc():
    assert next_call_time(MON_10_UTC, "Not/AZone", timedelta(0), 9, 19) == MON_10_UTC
