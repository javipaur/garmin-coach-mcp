import pytest

import server

_ROUTE = {
    "name": "Ruta 10K",
    "points": [{"lat": 40.0, "lon": -3.0}, {"lat": 40.001, "lon": -3.001}],
    "distance_km": 8.2,
}


def _stub_composition(monkeypatch):
    """Sustituye solo las llamadas externas; la orquestación bajo prueba es real."""
    calls = {
        "upload": [],
        "create": [],
        "schedule": [],
        "push": [],
        "map": [],
    }
    api = object()

    def fake_upload(route_data, name, description=""):
        calls["upload"].append({"route_data": route_data, "name": name, "description": description})
        return {"ok": True, "course_id": 999, "name": name}

    def fake_create(**kwargs):
        calls["create"].append(kwargs)
        return {"ok": True, "workout_id": 123, "name": kwargs["name"]}

    def fake_schedule(workout_id, target_date):
        calls["schedule"].append({"workout_id": workout_id, "target_date": target_date})
        return {
            "ok": True,
            "workout_id": workout_id,
            "date": target_date,
            "response": {"scheduledWorkoutId": 987},
        }

    def fake_push(pushed_api, workout_id):
        calls["push"].append({"api": pushed_api, "workout_id": workout_id})
        return {"messageId": 555}

    def fake_map(route_data, name=""):
        calls["map"].append({"route_data": route_data, "name": name})
        return {
            "viewer_url": "https://garmin.example.com/rutas#d=abc",
            "external_url": "https://www.openstreetmap.org/#map=14/40.0/-3.0",
        }

    monkeypatch.setattr(server, "upload_route_to_garmin", fake_upload)
    monkeypatch.setattr(server, "create_workout_from_description", fake_create)
    monkeypatch.setattr(server, "schedule_workout", fake_schedule)
    monkeypatch.setattr(server, "_get_api", lambda *_args, **_kwargs: api)
    monkeypatch.setattr(server, "_push_workout", fake_push)
    monkeypatch.setattr(server, "get_route_map_url", fake_map)
    return calls, api


def _stub_capable_device(monkeypatch, *, capable=True):
    monkeypatch.setattr(
        server,
        "get_devices",
        lambda: {
            "devices": [
                {
                    "deviceId": 3407704034,
                    "productDisplayName": "fenix 6X Pro",
                    "courseCapable": capable,
                    "customWorkoutCapable": capable,
                }
            ]
        },
    )
    monkeypatch.setattr(
        server,
        "get_device_last_used",
        lambda: {
            "device_last_used": {
                "userDeviceId": 3407704034,
                "deviceName": "fenix 6X Pro",
            }
        },
    )


def test_create_route_workout_requires_confirmation_before_touching_calendar(monkeypatch):
    calls, _api = _stub_composition(monkeypatch)

    with pytest.raises(RuntimeError, match="confirmar"):
        server.create_route_workout(
            route_data=_ROUTE,
            name="Ruta 10K",
            workout_description="8 km suaves",
            target_date="2026-11-23",
            confirm_calendar=False,
            push_to_device=False,
        )

    assert calls == {"upload": [], "create": [], "schedule": [], "push": [], "map": []}


def test_create_route_workout_creates_course_workout_and_shareable_map(monkeypatch):
    calls, _api = _stub_composition(monkeypatch)

    result = server.create_route_workout(
        route_data=_ROUTE,
        name="Ruta 10K",
        workout_description="8 km suaves",
        description="Circuitos reales",
        push_to_device=False,
    )

    assert result["ok"] is True
    assert result["course_id"] == 999
    assert result["workout_id"] == "123"
    assert result["scheduled"] is False
    assert result["pushed_to_device"] is False
    assert result["viewer_url"] == "https://garmin.example.com/rutas#d=abc"
    assert result["external_url"] == "https://www.openstreetmap.org/#map=14/40.0/-3.0"
    assert "Send to Device" in result["course_send_instructions"]
    assert calls["upload"] == [
        {
            "route_data": _ROUTE,
            "name": "Ruta 10K",
            "description": "Circuitos reales",
        }
    ]
    assert calls["create"] == [
        {
            "sport": "running",
            "name": "Ruta 10K",
            "description": "8 km suaves",
            "target_date": None,
            "auto_push_to_device": False,
        }
    ]
    assert calls["schedule"] == []
    assert calls["push"] == []
    assert calls["map"] == [{"route_data": _ROUTE, "name": "Ruta 10K"}]


def test_create_route_workout_schedules_only_after_explicit_confirmation(monkeypatch):
    calls, _api = _stub_composition(monkeypatch)

    result = server.create_route_workout(
        route_data=_ROUTE,
        name="Ruta 10K",
        workout_description="8 km suaves",
        target_date="2026-11-23",
        confirm_calendar=True,
        push_to_device=False,
    )

    assert result["scheduled"] is True
    assert result["schedule_id"] == 987
    assert result["scheduled_date"] == "2026-11-23"
    assert calls["schedule"] == [{"workout_id": "123", "target_date": "2026-11-23"}]
    assert calls["create"][0]["target_date"] is None


def test_create_route_workout_checks_device_capability_before_side_effects(monkeypatch):
    calls, api = _stub_composition(monkeypatch)
    _stub_capable_device(monkeypatch)

    result = server.create_route_workout(
        route_data=_ROUTE,
        name="Ruta 10K",
        workout_description="8 km suaves",
        push_to_device=True,
    )

    assert result["pushed_to_device"] is True
    assert result["push_device"] == "fenix 6X Pro"
    assert calls["push"] == [{"api": api, "workout_id": "123"}]
    assert len(calls["upload"]) == 1
    assert len(calls["create"]) == 1


def test_create_route_workout_refuses_push_without_a_capable_device(monkeypatch):
    calls, _api = _stub_composition(monkeypatch)
    _stub_capable_device(monkeypatch, capable=False)

    with pytest.raises(RuntimeError, match="compatible"):
        server.create_route_workout(
            route_data=_ROUTE,
            name="Ruta 10K",
            workout_description="8 km suaves",
            push_to_device=True,
        )

    assert calls["upload"] == []
    assert calls["create"] == []
    assert calls["push"] == []


def test_create_route_workout_reports_push_failure_without_losing_the_course(monkeypatch):
    calls, _api = _stub_composition(monkeypatch)
    _stub_capable_device(monkeypatch)

    def failing_push(_api, workout_id):
        calls["push"].append({"api": _api, "workout_id": workout_id})
        raise RuntimeError("reloj apagado")

    monkeypatch.setattr(server, "_push_workout", failing_push)

    result = server.create_route_workout(
        route_data=_ROUTE,
        name="Ruta 10K",
        workout_description="8 km suaves",
        push_to_device=True,
    )

    assert result["ok"] is True
    assert result["course_id"] == 999
    assert result["workout_id"] == "123"
    assert result["pushed_to_device"] is False
    assert "reloj apagado" in result["push_error"]
