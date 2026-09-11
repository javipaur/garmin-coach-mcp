from datetime import datetime, timedelta, timezone

from recovery import _extract_latest_activity_end_local, _safe_float


def test_safe_float_number():
    assert _safe_float("12.5") == 12.5
    assert _safe_float(30) == 30.0


def test_safe_float_none():
    assert _safe_float(None) is None


def test_safe_float_bool_excluded():
    assert _safe_float(True) is None
    assert _safe_float(False) is None


def test_safe_float_invalid():
    assert _safe_float("abc") is None


def test_extract_latest_activity_end_local_none():
    assert _extract_latest_activity_end_local(None) is None
    assert _extract_latest_activity_end_local({"no": "data"}) is None


def test_extract_latest_activity_end_local_picks_latest():
    raw = {
        "recent_activities_raw": [
            {"endTimeLocal": "2026-09-10T12:00:00", "duration": 3600},
            {"endTimeLocal": "2026-09-11T08:30:00", "duration": 1800},
        ]
    }
    out = _extract_latest_activity_end_local(raw)
    assert out is not None
    assert out == datetime(2026, 9, 11, 8, 30, tzinfo=timezone(timedelta(hours=2)))


def test_extract_latest_activity_end_local_from_duration():
    raw = {
        "recent_activities_raw": [
            {"startTimeLocal": "2026-09-10T10:00:00", "duration": 3600},
        ]
    }
    out = _extract_latest_activity_end_local(raw)
    assert out is not None
    assert out == datetime(2026, 9, 10, 11, 0, tzinfo=timezone(timedelta(hours=2)))
