from localization import (
    _GARMIN_ES,
    ES_FIELD_LABELS,
    _build_sleep_safe_text,
    _normalize_readiness_status_es,
    _translate_garmin,
)


def test_translate_garmin_basic():
    assert _translate_garmin({"status": "HIGH"}) == {"status": "Alto"}


def test_translate_garmin_nested():
    obj = {"a": {"b": ["BALANCED", "REST"]}, "c": "UNKNOWN_VAL"}
    out = _translate_garmin(obj)
    assert out["a"]["b"] == ["Equilibrado", "Descanso"]
    assert out["c"] == "UNKNOWN_VAL"


def test_translate_garmin_non_dict():
    assert _translate_garmin("HIGH") == "Alto"
    assert _translate_garmin(42) == 42


def test_translate_garmin_deep_recursion_safe():
    deep = {"x": {}}
    current = deep["x"]
    for _ in range(60):
        current["x"] = {}
        current = current["x"]
    _translate_garmin(deep)


def test_garmin_es_dictionary_has_required_keys():
    for key in ("HIGH", "LOW", "BALANCED", "PRODUCTIVE", "RECOVERY", "REST"):
        assert key in _GARMIN_ES


def test_normalize_readiness_status_es():
    assert _normalize_readiness_status_es("low") == "Baja"
    assert _normalize_readiness_status_es("MODERATE") == "Moderada"
    assert _normalize_readiness_status_es(None) is None
    assert _normalize_readiness_status_es("custom") == "custom"


def test_build_sleep_safe_text():
    assert _build_sleep_safe_text(80, "7h 30m") == "80 puntos y 7h 30m"
    assert _build_sleep_safe_text(80, None) == "80 puntos"
    assert _build_sleep_safe_text(None, "7h") == "7h"
    assert _build_sleep_safe_text(None, None) is None


def test_es_field_labels_non_empty():
    assert isinstance(ES_FIELD_LABELS, dict)
    assert len(ES_FIELD_LABELS) > 0
