from copy import deepcopy

import pytest

import server


class _FakeExtrasApi:
    def __init__(self):
        self.workout = {
            "workoutId": 123,
            "workoutName": "Entreno viejo",
            "steps": [{"type": "interval", "duration": 300}],
        }
        self.updated = None
        self.deleted_activity = None

    def get_workout_by_id(self, workout_id):
        assert str(workout_id) == "123"
        return deepcopy(self.workout)

    def update_workout(self, workout_id, workout_json):
        self.updated = (workout_id, workout_json)
        return workout_json

    def delete_activity(self, activity_id):
        self.deleted_activity = activity_id
        return {}


@pytest.fixture
def extras_api(monkeypatch):
    api = _FakeExtrasApi()
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    return api


def test_rename_workout_keeps_id_and_steps(extras_api):
    result = server.rename_workout("123", "Entreno nuevo")

    assert result == {
        "ok": True,
        "workout_id": "123",
        "old_name": "Entreno viejo",
        "new_name": "Entreno nuevo",
    }
    workout_id, payload = extras_api.updated
    assert workout_id == "123"
    assert payload["workoutId"] == 123
    assert payload["workoutName"] == "Entreno nuevo"
    assert payload["steps"] == [{"type": "interval", "duration": 300}]


def test_rename_workout_rejects_blank_name(extras_api):
    with pytest.raises(ValueError, match="nombre"):
        server.rename_workout("123", "   ")

    assert extras_api.updated is None


def test_delete_activity_requires_explicit_confirmation(extras_api):
    result = server.delete_activity("555")

    assert result == {"ok": True, "dry_run": True, "activity_id": "555", "deleted": 0}
    assert extras_api.deleted_activity is None


def test_delete_activity_deletes_when_confirmed(extras_api):
    result = server.delete_activity("555", confirm=True)

    assert result == {"ok": True, "dry_run": False, "activity_id": "555", "deleted": 1}
    assert extras_api.deleted_activity == "555"


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def _weather_payload():
    return {
        "timezone": "Europe/Madrid",
        "daily": {
            "time": ["2026-11-22", "2026-11-23"],
            "temperature_2m_max": [12.5, 9.0],
            "temperature_2m_min": [3.0, 1.5],
            "precipitation_probability_max": [10, 70],
            "wind_speed_10m_max": [8.0, 22.0],
            "weather_code": [1, 3],
        },
    }


def test_get_route_weather_infers_center_from_route_points(monkeypatch):
    calls = {}

    def fake_get(url, params=None, timeout=None):
        calls.update(url=url, params=params, timeout=timeout)
        return _FakeResponse(_weather_payload())

    monkeypatch.setattr(server.requests, "get", fake_get)
    route = {
        "points": [
            {"lat": 42.8, "lon": -2.7},
            {"lat": 42.9, "lon": -2.6},
        ]
    }

    result = server.get_route_weather(route_data=route)

    assert "open-meteo.com" in calls["url"]
    assert calls["params"]["latitude"] == pytest.approx(42.85)
    assert calls["params"]["longitude"] == pytest.approx(-2.65)
    assert calls["timeout"] <= 15
    assert result["source"] == "open-meteo"
    assert result["days"][0] == {
        "date": "2026-11-22",
        "temp_max_c": 12.5,
        "temp_min_c": 3.0,
        "precip_prob_max": 10,
        "wind_kmh": 8.0,
        "weather_code": 1,
    }


def test_get_route_weather_can_filter_by_date(monkeypatch):
    monkeypatch.setattr(
        server.requests,
        "get",
        lambda *args, **kwargs: _FakeResponse(_weather_payload()),
    )

    result = server.get_route_weather(lat=42.84, lon=-2.68, date="2026-11-23")

    assert len(result["days"]) == 1
    assert result["days"][0]["date"] == "2026-11-23"


def test_get_route_weather_reports_network_errors_clearly(monkeypatch):
    def fake_get(*args, **kwargs):
        raise server.requests.exceptions.Timeout("tiempo agotado")

    monkeypatch.setattr(server.requests, "get", fake_get)

    with pytest.raises(RuntimeError, match="tiempo"):
        server.get_route_weather(lat=42.84, lon=-2.68)
