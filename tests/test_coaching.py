from coaching import (
    _decision_level,
    _decision_num,
    _decision_pick_primary_driver,
    _decision_recommendation_text,
)


def test_decision_num():
    assert _decision_num("12.5") == 12.5
    assert _decision_num(0) == 0.0
    assert _decision_num(None) is None
    assert _decision_num("abc") is None


def test_pick_primary_driver_readiness():
    ctx = {"training_readiness": 30}
    assert _decision_pick_primary_driver(ctx, None, None) == "Predisposición para entrenar baja o moderada-baja"


def test_pick_primary_driver_bb():
    ctx = {"body_battery_current": 20}
    assert _decision_pick_primary_driver(ctx, None, None) == "Body Battery bajo"


def test_pick_primary_driver_sleep():
    ctx = {"sleep_score": 50}
    assert _decision_pick_primary_driver(ctx, None, None) == "Sueño mejorable"


def test_pick_primary_driver_run_load():
    ctx = {}
    latest_run = {"training_load": 250}
    assert _decision_pick_primary_driver(ctx, latest_run, None) == "La última sesión endurance fue exigente"


def test_pick_primary_driver_fallback():
    ctx = {"acute_load": 100}
    assert _decision_pick_primary_driver(ctx, None, None) == "Carga aguda reciente"


def test_decision_level_keys():
    ctx = {"acute_load": 100}
    level, color, tag = _decision_level(ctx, None, None)
    assert isinstance(level, str)
    assert isinstance(color, str)
    assert isinstance(tag, str)


def test_decision_recommendation_text_default():
    text = _decision_recommendation_text("critical_alarm", None, None)
    assert isinstance(text, str)
    assert len(text) > 0
