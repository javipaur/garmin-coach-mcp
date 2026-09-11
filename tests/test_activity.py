from activity import (
    _activity_family,
    _normalize_activity,
    _pick_activity_metadata,
    _pick_activity_summary,
)


def test_activity_family_categories():
    assert _activity_family("running") == "endurance"
    assert _activity_family("cycling") == "cycling"
    assert _activity_family("strength_training") == "strength"
    assert _activity_family(None) == "other"
    assert _activity_family("unknown_type") == "unknown_type"


def test_normalize_activity_basic():
    activity = {
        "activityId": 123,
        "activityName": "Carrera",
        "activityType": {"typeKey": "running"},
        "duration": 3600,
        "distance": 10000,
        "startTimeLocal": "2026-09-10T08:00:00",
        "averageHR": 150,
        "summaryDTO": {"calories": 600},
    }
    out = _normalize_activity(activity)
    assert out["activity_id"] == 123
    assert out["type"] == "running"
    assert out["activity_family"] == "endurance"
    assert out["duration_min"] == 60.0
    assert out["distance_km"] == 10.0
    assert out["tipo_actividad"] == "Correr"


def test_normalize_activity_fallback_summary():
    activity = {
        "activityId": 1,
        "activityType": {"typeKey": "cycling"},
        "summaryDTO": {"duration": 1800, "distance": 20000},
    }
    out = _normalize_activity(activity)
    assert out["duration_min"] == 30.0
    assert out["distance_km"] == 20.0


def test_pick_activity_summary_filters_none():
    summary = {"distance": 10.0, "duration": 3600, "calories": None}
    out = _pick_activity_summary(summary)
    assert "distance" in out
    assert "duration" in out
    assert "calories" not in out


def test_pick_activity_summary_non_dict():
    assert _pick_activity_summary(None) == {}
    assert _pick_activity_summary("x") == {}


def test_pick_activity_metadata_filters_none():
    metadata = {"distance": 10.0, "averagePower": None, "calories": 300}
    out = _pick_activity_metadata(metadata)
    assert "distance" in out
    assert "calories" in out
    assert "averagePower" not in out
