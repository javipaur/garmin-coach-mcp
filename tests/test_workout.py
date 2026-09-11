from workout import (
    _garmin_workout_step_from_desc,
    _parse_distance_to_m,
    _parse_duration_min,
    _parse_workout_steps_text,
)


def test_parse_distance_to_m_km():
    assert _parse_distance_to_m("5 km") == 5000.0
    assert _parse_distance_to_m("Corre 10km") == 10000.0


def test_parse_distance_to_m_meters():
    assert _parse_distance_to_m("150 metros") == 150.0


def test_parse_distance_to_m_invalid():
    assert _parse_distance_to_m("") is None
    assert _parse_distance_to_m("sin datos") is None


def test_parse_duration_min_hours():
    assert _parse_duration_min("1h 30min") == 60.0


def test_parse_duration_min_minutes():
    assert _parse_duration_min("10 minutos") == 10.0


def test_parse_duration_min_invalid():
    assert _parse_duration_min("nada") is None


def test_parse_workout_steps_text_full():
    desc = """Calentamiento 5 min
20 min z2
Descanso 2 min
Vuelta a la calma 5 min"""
    steps = _parse_workout_steps_text(desc)
    assert len(steps) == 4
    assert steps[0]["type"] == "warmup"
    assert steps[0]["duration_min"] == 5
    assert steps[1]["type"] == "active"
    assert steps[1]["duration_min"] == 20
    assert steps[2]["type"] == "rest"
    assert steps[2]["duration_min"] == 2
    assert steps[3]["type"] == "cooldown"


def test_parse_workout_steps_text_hr_zone():
    desc = "3 km zona 3"
    steps = _parse_workout_steps_text(desc)
    assert steps[0]["target_hr_zone"] == 3
    assert steps[0]["distance_km"] == 3.0


def test_workout_step_from_desc_basic():
    step = _garmin_workout_step_from_desc({"type": "warmup", "duration_min": 5}, 1)
    assert step["type"] == "ExecutableStepDTO"
    assert step["stepOrder"] == 1
    assert step["stepType"]["stepTypeKey"] == "warmup"
    assert step["endCondition"]["conditionTypeKey"] == "time"
    assert step["endConditionValue"] == 300.0


def test_workout_step_from_desc_hr_zone():
    step = _garmin_workout_step_from_desc({"type": "active", "duration_sec": 60, "target_hr_zone": 3}, 2)
    assert step["stepType"]["stepTypeKey"] == "interval"
    assert step["targetType"]["workoutTargetTypeKey"] == "heart.rate.zone"
    assert step["zoneNumber"] == 3


def test_workout_step_from_desc_distance():
    step = _garmin_workout_step_from_desc({"type": "active", "distance_m": 1000}, 3)
    assert step["endCondition"]["conditionTypeKey"] == "distance"
    assert step["endConditionValue"] == 1000.0
