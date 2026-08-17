"""The daily-rotation cutoff arithmetic in the API's session sweeper."""

from datetime import datetime, time, timedelta, timezone

from api.main import _last_rotation_instant


def test_cutoff_is_today_when_time_has_passed() -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    cutoff = _last_rotation_instant(now, time(3, 30))
    assert cutoff == datetime(2026, 8, 16, 3, 30, tzinfo=timezone.utc)


def test_cutoff_is_yesterday_before_the_time() -> None:
    now = datetime(2026, 8, 16, 2, 0, tzinfo=timezone.utc)
    cutoff = _last_rotation_instant(now, time(3, 30))
    assert cutoff == datetime(2026, 8, 15, 3, 30, tzinfo=timezone.utc)


def test_cutoff_at_the_exact_instant_is_now() -> None:
    now = datetime(2026, 8, 16, 3, 30, tzinfo=timezone.utc)
    assert _last_rotation_instant(now, time(3, 30)) == now


def test_midnight_rotation() -> None:
    now = datetime(2026, 8, 16, 0, 0, 30, tzinfo=timezone.utc)
    cutoff = _last_rotation_instant(now, time(0, 0))
    assert cutoff == datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)
    assert now - cutoff == timedelta(seconds=30)
