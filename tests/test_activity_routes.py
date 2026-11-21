import pytest

import server


def _track_points():
    # 0.009 grados de latitud ≈ 1 km entre puntos: 4 puntos = 3 km.
    return [
        {"lat": 42.8400, "lon": -2.6800},
        {"lat": 42.8490, "lon": -2.6800},
        {"lat": 42.8580, "lon": -2.6800},
        {"lat": 42.8670, "lon": -2.6800},
    ]


class _FakeGarminApi:
    def __init__(self, activities, details_by_id):
        self.activities = activities
        self.details_by_id = details_by_id

    def get_activities(self, start, limit):
        return self.activities

    def get_activity_details(self, activity_id):
        return self.details_by_id.get(str(activity_id), {})


@pytest.fixture
def real_track_api(monkeypatch):
    activities = [
        {
            "activityId": 111,
            "activityName": "Vuelta habitual",
            "activityType": {"typeKey": "running"},
            "summaryDTO": {"distance": 3000.0, "elevationGain": 12.0},
            "startTimeLocal": "2026-11-20T18:00:00.0",
        }
    ]
    details = {
        "111": {
            "geoPolylineDTO": {"polyline": _track_points()},
            "summaryDTO": {"elevationGain": 12.0},
        }
    }
    api = _FakeGarminApi(activities, details)
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    return api


def test_extract_track_points_reads_real_geo_polyline_structure():
    details = {"geoPolylineDTO": {"polyline": _track_points()}}

    points = server._extract_track_points(details)

    assert points == _track_points()


def test_routes_from_activity_tracks_returns_route_with_track_points(real_track_api):
    routes = server._routes_from_activity_tracks(distance_km=3.0, max_results=2)

    assert len(routes) == 1
    route = routes[0]
    assert route["points"] == _track_points()
    assert route["distance_km"] == pytest.approx(3.0, abs=0.1)
    assert route["point_count"] == 4
    assert route["deviation_pct"] == pytest.approx(0.0, abs=1)
    assert route["source"] == "activity_track"
    assert route["activity_id"] == "111"


def test_routes_from_activity_tracks_discards_tracks_far_from_target(real_track_api):
    routes = server._routes_from_activity_tracks(distance_km=10.0, max_results=2)

    assert routes == []


def test_suggest_routes_from_profile_prefers_real_tracks_over_osm(real_track_api, monkeypatch):
    monkeypatch.setattr(server, "get_user_profile", lambda: {"home_lat": 42.84, "home_lon": -2.68})

    def forbidden_osm_call(**kwargs):
        raise AssertionError("No debería llamar a OSM cuando hay tracks reales")

    monkeypatch.setattr(server, "suggest_routes", forbidden_osm_call)

    result = server.suggest_routes_from_profile(distance_km=3.0)

    assert result["count"] == 1
    assert result["routes"][0]["source"] == "activity_track"
    assert result["query"]["lat"] == pytest.approx(42.84)
    assert result["query"]["lon"] == pytest.approx(-2.68)


def test_suggest_routes_from_profile_falls_back_to_osm_without_tracks(monkeypatch):
    monkeypatch.setattr(server, "get_user_profile", lambda: {"home_lat": 42.84, "home_lon": -2.68})
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_routes_from_activity_tracks", lambda **kwargs: [])
    calls = []

    def fake_suggest_routes(**kwargs):
        calls.append(kwargs)
        return {"query": {"lat": kwargs["lat"], "lon": kwargs["lon"]}, "routes": [], "count": 0}

    monkeypatch.setattr(server, "suggest_routes", fake_suggest_routes)

    result = server.suggest_routes_from_profile(distance_km=8.0)

    assert calls
    assert result["count"] == 0
