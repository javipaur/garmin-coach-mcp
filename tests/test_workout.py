import pytest

from workout import (
    _garmin_workout_step_from_desc,
    _garmin_workout_steps_from_desc,
    _parse_distance_to_m,
    _parse_duration_min,
    _parse_pace_range,
    _parse_workout_steps_text,
)


def test_parse_distance_to_m_km():
    assert _parse_distance_to_m("5 km") == 5000.0
    assert _parse_distance_to_m("Corre 10km") == 10000.0
    assert _parse_distance_to_m("4 k") == 4000.0


def test_parse_distance_to_m_meters():
    assert _parse_distance_to_m("150 m") == 150.0


def test_parse_distance_to_m_invalid():
    assert _parse_distance_to_m("") is None
    assert _parse_distance_to_m("sin datos") is None


def test_parse_duration_min_hours():
    assert _parse_duration_min("1h 30min") == 30.0


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
    desc = "3 km Z3"
    steps = _parse_workout_steps_text(desc)
    assert steps[0]["target_hr_zone"] == 3
    assert steps[0]["distance_km"] == 3.0


def test_parse_workout_steps_text_series():
    desc = "3x800m Z4, rec 400m"
    steps = _parse_workout_steps_text(desc)
    assert len(steps) == 1
    group = steps[0]
    assert group["type"] == "repeat"
    assert group["iterations"] == 3
    assert len(group["steps"]) == 2
    interval, rest = group["steps"]
    assert interval["type"] == "interval"
    assert interval["distance_m"] == 800
    assert interval["target_hr_zone"] == 4
    assert rest["type"] == "rest"
    assert rest["distance_m"] == 400


def test_parse_workout_steps_text_series_time():
    desc = "4x1km, descanso 2min"
    steps = _parse_workout_steps_text(desc)
    assert len(steps) == 1
    group = steps[0]
    assert group["type"] == "repeat"
    assert group["iterations"] == 4
    assert group["steps"][0]["distance_km"] == 1.0
    assert group["steps"][1]["duration_min"] == 2.0


def test_parse_workout_steps_text_standalone_zone():
    desc = "10 min Z2\nZ4\n8 min"
    steps = _parse_workout_steps_text(desc)
    assert len(steps) == 2
    assert steps[0]["duration_min"] == 10
    assert steps[0]["target_hr_zone"] == 4
    assert steps[1]["type"] == "active"
    assert steps[1]["duration_min"] == 8


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
    assert step["endCondition"]["conditionTypeId"] == 3
    assert step["endCondition"]["conditionTypeKey"] == "distance"
    assert step["endCondition"]["displayOrder"] == 3
    assert step["endConditionValue"] == 1000.0


def test_parse_pace_range():
    low, high = _parse_pace_range("ritmo 4:10-4:15/km")
    assert low == pytest.approx(1000 / (4 * 60 + 15), abs=0.001)
    assert high == pytest.approx(1000 / (4 * 60 + 10), abs=0.001)
    assert low < high

    assert _parse_pace_range("4:30/km") == pytest.approx(
        (1000 / (4 * 60 + 30), 1000 / (4 * 60 + 30)), abs=0.001
    )
    assert _parse_pace_range("entre 4:30 y 4:35") == pytest.approx(
        (1000 / (4 * 60 + 35), 1000 / (4 * 60 + 30)), abs=0.001
    )
    assert _parse_pace_range("de 4:20 a 4:25/km") == pytest.approx(
        (1000 / (4 * 60 + 25), 1000 / (4 * 60 + 20)), abs=0.001
    )
    assert _parse_pace_range("sin ritmo") is None


def test_parse_workout_steps_text_pace_range_in_spanish():
    steps = _parse_workout_steps_text("4 km entre 4:30 y 4:35")
    assert steps == [
        {"type": "active", "distance_km": 4.0, "target_pace": "4:30 y 4:35"}
    ]


def test_garmin_pace_target_uses_fast_value_first():
    """Garmin descarta los valores si targetValueOne no es el ritmo más rápido."""
    garmin = _garmin_workout_step_from_desc(
        {"type": "active", "distance_km": 4, "target_pace": "entre 4:30 y 4:35"}, 1
    )
    assert garmin["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert garmin["targetValueOne"] == pytest.approx(1000 / 270, abs=0.001)
    assert garmin["targetValueTwo"] == pytest.approx(1000 / 275, abs=0.001)
    assert garmin["targetValueOne"] > garmin["targetValueTwo"]


def test_parse_workout_steps_text_rest_seconds():
    steps = _parse_workout_steps_text("3x800m rec 90s")
    assert steps[0]["steps"][1]["duration_min"] == pytest.approx(1.5)


def test_parse_workout_steps_text_series_pace():
    desc = "Calentamiento 10 min, 5x1000m ritmo 4:10-4:15/km, rec 2min, Vuelta a la calma 5 min"
    steps = _parse_workout_steps_text(desc)
    assert [s["type"] for s in steps] == ["warmup", "repeat", "cooldown"]
    group = steps[1]
    assert group["iterations"] == 5
    assert group["steps"][0]["target_pace"] == "4:10-4:15/km"
    assert group["steps"][1]["duration_min"] == 2.0


def test_garmin_workout_steps_from_desc_repeat_group():
    steps = [
        {"type": "warmup", "duration_min": 10},
        {
            "type": "repeat",
            "iterations": 5,
            "steps": [
                {"type": "interval", "distance_km": 1, "target_pace": "4:10-4:15/km"},
                {"type": "rest", "duration_min": 2},
            ],
        },
        {"type": "cooldown", "duration_min": 5},
    ]
    garmin = _garmin_workout_steps_from_desc(steps)
    assert [s["type"] for s in garmin] == [
        "ExecutableStepDTO",
        "RepeatGroupDTO",
        "ExecutableStepDTO",
    ]
    group = garmin[1]
    assert group["stepOrder"] == 2
    assert group["stepType"] == {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6}
    assert group["numberOfIterations"] == 5
    assert group["endCondition"]["conditionTypeId"] == 7
    assert group["endCondition"]["conditionTypeKey"] == "iterations"
    assert group["endConditionValue"] == 5.0
    assert group["smartRepeat"] is False

    interval, recovery = group["workoutSteps"]
    assert interval["stepOrder"] == 3
    assert interval["endCondition"]["conditionTypeId"] == 3
    assert interval["endCondition"]["conditionTypeKey"] == "distance"
    assert interval["endConditionValue"] == 1000.0
    assert interval["targetType"] == {
        "workoutTargetTypeId": 6,
        "workoutTargetTypeKey": "pace.zone",
        "displayOrder": 1,
    }
    assert interval["targetValueOne"] == pytest.approx(1000 / 250, abs=0.001)
    assert interval["targetValueTwo"] == pytest.approx(1000 / 255, abs=0.001)

    assert recovery["stepOrder"] == 4
    assert recovery["stepType"]["stepTypeKey"] == "recovery"
    assert recovery["endConditionValue"] == 120.0

    assert garmin[2]["stepOrder"] == 5
    assert garmin[2]["stepType"]["stepTypeKey"] == "cooldown"


def test_garmin_workout_steps_from_desc_accepts_json_repeat_keys():
    steps = [
        {
            "type": "REPEAT",
            "reps": 3,
            "workout_steps": [
                {"type": "active", "distance_m": 400, "target_pace_mps": 3.9},
                {"type": "recuperacion", "duration_sec": 90},
            ],
        }
    ]
    garmin = _garmin_workout_steps_from_desc(steps)
    group = garmin[0]
    assert group["numberOfIterations"] == 3
    assert group["workoutSteps"][0]["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert group["workoutSteps"][0]["targetValueOne"] == pytest.approx(3.9)
    assert group["workoutSteps"][1]["stepType"]["stepTypeKey"] == "recovery"
