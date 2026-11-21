import server


class _FakeApi:
    def __init__(self):
        self.uploaded = None

    def upload_workout(self, payload):
        self.uploaded = payload
        return {"workoutId": 123}


def test_create_workout_from_description_uploads_repeat_group(monkeypatch):
    api = _FakeApi()
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id: api)

    result = server.create_workout_from_description(
        sport="running",
        name="Series 5x1000",
        description=(
            "Calentamiento 10 min, "
            "5x1000m ritmo 4:10-4:15/km, rec 2min, "
            "Vuelta a la calma 5 min"
        ),
    )

    assert result["ok"] is True
    steps = api.uploaded["workoutSegments"][0]["workoutSteps"]
    assert [s["type"] for s in steps] == [
        "ExecutableStepDTO",
        "RepeatGroupDTO",
        "ExecutableStepDTO",
    ]
    group = steps[1]
    assert group["numberOfIterations"] == 5
    interval, rest = group["workoutSteps"]
    assert interval["endCondition"]["conditionTypeKey"] == "distance"
    assert interval["endCondition"]["conditionTypeId"] == 3
    assert interval["targetType"]["workoutTargetTypeKey"] == "pace.zone"
    assert rest["endConditionValue"] == 120.0
