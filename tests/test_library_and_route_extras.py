import asyncio
import json
from datetime import date
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

import server


class _FakeDeleteApi:
    def __init__(self):
        self.deleted = []

    @property
    def client(self):
        return self

    def delete(self, domain, path, **kwargs):
        self.deleted.append((domain, path, kwargs))
        return None


@pytest.fixture
def delete_api(monkeypatch):
    api = _FakeDeleteApi()
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    return api


def test_delete_course_is_dry_run_by_default(delete_api):
    result = server.delete_course("531605238")

    assert result == {"ok": True, "dry_run": True, "course_id": "531605238", "deleted": 0}
    assert delete_api.deleted == []


def test_delete_course_deletes_when_confirmed(delete_api):
    result = server.delete_course("531605238", confirm=True)

    assert result == {"ok": True, "dry_run": False, "course_id": "531605238", "deleted": 1}
    assert delete_api.deleted == [
        ("connectapi", "course-service/course/531605238", {"timeout": 30})
    ]


def test_delete_courses_batches_and_validates(delete_api):
    result = server.delete_courses(["531605238", "531580081"], confirm=True)

    assert result["deleted"] == 2
    assert [call[1] for call in delete_api.deleted] == [
        "course-service/course/531605238",
        "course-service/course/531580081",
    ]

    with pytest.raises(ValueError, match="ID de course"):
        server.delete_course("not-a-number")


def test_get_route_gpx_url_is_absolute_and_encodes_name(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://garmin.example.com/")
    route = {
        "name": "Ruta 10K",
        "points": [{"lat": 40.416775, "lon": -3.70379}, {"lat": 40.42, "lon": -3.71}],
    }

    result = server.get_route_gpx_url(route)

    assert result["gpx_url"].startswith("https://garmin.example.com/rutas/gpx?d=")
    assert "n=Ruta%2010K" in result["gpx_url"]
    assert result["point_count"] == 2


def test_gpx_payload_from_query_decodes_polyline():
    coords = [(42.83741, -2.68652), (42.84, -2.69)]
    encoded = server._encode_polyline(coords)

    xml, filename = server._gpx_payload_from_query({"d": encoded, "n": "Vuelta Vitoria"})

    assert '<trkpt lat="42.83741" lon="-2.68652"' in xml
    assert filename == "Vuelta-Vitoria.gpx"
    with pytest.raises(ValueError, match="no válida"):
        server._gpx_payload_from_query({"d": ""})


def test_route_gpx_download_returns_attachment():
    coords = [(42.837408, -2.686516), (42.84, -2.69)]
    query = urlencode({"d": server._encode_polyline(coords), "n": "Vuelta Vitoria"})
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/rutas/gpx",
        "query_string": query.encode("ascii"),
        "scheme": "https",
        "server": ("testserver", 443),
        "headers": [(b"host", b"garmin.example.com")],
    }

    response = asyncio.run(server.route_gpx_download(Request(scope)))

    assert response.status_code == 200
    assert response.media_type == "application/gpx+xml"
    assert 'attachment; filename="Vuelta-Vitoria.gpx"' in response.headers["content-disposition"]
    assert b"<trkpt" in response.body


def test_route_map_has_gpx_download_link_and_route_is_registered():
    html = server._render_route_map_html()
    paths = [getattr(r, "path", None) for r in server.mcp._get_additional_http_routes()]

    assert "Descargar GPX" in html
    assert "/rutas/gpx" in html
    assert "/rutas/gpx" in paths


@pytest.fixture
def long_run_api(monkeypatch):
    api = _FakeDeleteApi()
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    monkeypatch.setattr(server, "_today_local", lambda: date(2026, 11, 21))
    api.get_workouts_calendar = lambda _y, _m: [
        {"date": "2026-11-22", "workoutName": "S10 Sab - 17km 2x3k", "itemType": "workout"}
    ]
    return api


def _weather(days):
    return {
        "source": "open-meteo",
        "lat": 42.84,
        "lon": -2.68,
        "days": [
            {
                "date": d,
                "temp_max_c": t,
                "temp_min_c": 5,
                "precip_prob_max": p,
                "wind_kmh": w,
                "weather_code": c,
            }
            for d, t, p, w, c in days
        ],
    }


def test_recommend_long_run_day_penalizes_rain_wind_and_busy_days(monkeypatch, long_run_api):
    forecast = _weather(
        [
            ("2026-11-21", 12, 30, 20, 3),
            ("2026-11-22", 10, 90, 40, 63),  # día ocupado y mal tiempo
            ("2026-11-23", 11, 40, 25, 3),
            ("2026-11-24", 15, 0, 5, 0),  # día ideal
            ("2026-11-25", 10, 60, 30, 51),
            ("2026-11-26", 9, 50, 35, 61),
            ("2026-11-27", 13, 20, 15, 2),
        ]
    )
    monkeypatch.setattr(server, "get_route_weather", lambda *a, **k: forecast)

    result = server.recommend_long_run_day(lat=42.84, lon=-2.68)

    assert result["best"]["date"] == "2026-11-24"
    assert result["best"]["score"] == pytest.approx(min(d["score"] for d in result["ranked"]))
    busy = next(d for d in result["ranked"] if d["date"] == "2026-11-22")
    assert any("planificado" in reason for reason in busy["reasons"])
    assert not hasattr(long_run_api, "schedule_workout")  # no toca el calendario
    assert result["note"] == "Solo recomienda; no modifica el calendario"


def test_recommend_long_run_day_accepts_route_data(monkeypatch, long_run_api):
    calls = {}

    def fake_weather(**kwargs):
        calls.update(kwargs)
        return _weather([("2026-11-22", 15, 0, 5, 0)])

    monkeypatch.setattr(server, "get_route_weather", fake_weather)
    route = {
        "points": [
            {"lat": 42.8, "lon": -2.7},
            {"lat": 42.9, "lon": -2.6},
        ]
    }

    result = server.recommend_long_run_day(route_data=route, days=2)

    assert calls["lat"] == pytest.approx(42.85)
    assert calls["lon"] == pytest.approx(-2.65)
    assert len(result["ranked"]) == 1


class _FakeLibraryApi:
    def __init__(self):
        self.workouts = [
            {"workoutId": 101, "workoutName": "Rodaje 8km"},
            {"workoutId": 102, "workoutName": "rodaje   8km"},
            {"workoutId": 103, "workoutName": "Series 5x1000"},
        ]

    def get_workouts(self, start, limit):
        return self.workouts[start : start + limit]

    def get_workout_by_id(self, wid):
        steps = (
            [{"stepType": "interval", "duration": 300, "targetValueOne": 4.5}]
            if str(wid) != "103"
            else [{"stepType": "interval", "duration": 600, "targetValueOne": 5.0}]
        )
        return {"workoutId": int(wid), "workoutSteps": steps}


@pytest.fixture
def library_api(monkeypatch):
    api = _FakeLibraryApi()
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    return api


def test_find_duplicate_workouts_groups_by_name_and_steps(library_api):
    result = server.find_duplicate_workouts()

    assert result["count"] == 1
    group = result["duplicates"][0]
    assert group["workout_ids"] == [101, 102]
    assert group["reasons"] == ["name", "steps"]
    assert group["suggested_keep"] == 102
    assert group["suggested_delete"] == [101]
    assert group["name"] == "Rodaje 8km"


def test_find_duplicate_workouts_can_filter_by_name(library_api):
    result = server.find_duplicate_workouts(by="name")

    assert result["count"] == 1
    assert result["duplicates"][0]["reasons"] == ["name"]


def test_find_duplicate_workouts_reports_empty_library(monkeypatch):
    monkeypatch.setattr(server, "get_workout_library", lambda **_kwargs: {"workouts": []})

    result = server.find_duplicate_workouts()

    assert result == {"count": 0, "duplicates": [], "scanned": 0}


def test_steps_signature_is_stable_across_key_order():
    a = {"workoutSteps": [{"stepType": "interval", "duration": 300, "endCondition": "time"}]}
    b = {"workoutSteps": [{"endCondition": "time", "duration": 300, "stepType": "interval"}]}

    assert server._steps_signature(a) == server._steps_signature(b)
    assert json.loads(server._steps_signature(a))[0]["stepType"] == "interval"
