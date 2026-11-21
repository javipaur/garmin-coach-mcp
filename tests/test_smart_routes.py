import networkx as nx
import pytest

import server


def _mini_graph():
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "EPSG:4326"
    coords = {
        0: (40.0000, -3.0000, 520),
        1: (40.0050, -3.0000, 525),
        2: (40.0050, -3.0070, 523),
        3: (40.0000, -3.0070, 520),
    }
    for node, (lat, lon, elevation) in coords.items():
        graph.add_node(node, y=lat, x=lon, elevation=elevation)
    edges = [
        (0, 1, 550, "asphalt"),
        (1, 2, 600, "asphalt"),
        (2, 3, 550, "dirt"),
        (3, 0, 550, "asphalt"),
        (2, 0, 550, "dirt"),
        (0, 2, 550, "dirt"),
    ]
    for u, v, length, surface in edges:
        graph.add_edge(u, v, length=length, surface=surface)
        graph.add_edge(v, u, length=length, surface=surface)
    return graph


@pytest.fixture
def mini_graph(monkeypatch):
    graph = _mini_graph()
    monkeypatch.setattr(server, "_get_route_graph", lambda *a, **k: graph)
    return graph


def test_generate_loop_route_builds_a_real_loop_with_surface_and_elevation(mini_graph):
    routes = server._generate_loop_route(
        40.0, -3.0, target_distance_km=1.7, network_type="walk", max_results=3
    )

    assert routes
    route = routes[0]
    assert route["distance_km"] == pytest.approx(1.7, abs=0.26)
    assert route["is_loop"] is True
    assert route["surface"] in {"asphalt", "dirt", "mixed"}
    assert route["elevation_gain_m"] == pytest.approx(5)
    assert route["points"][0] == route["points"][-1]
    assert len({(p["lat"], p["lon"]) for p in route["points"]}) >= 3


class _FakeActivityApi:
    def __init__(self, activities):
        self.activities = activities

    def get_activities(self, start, limit):
        return self.activities


@pytest.fixture
def activity_clusters(monkeypatch):
    activities = [
        {
            "activityId": i,
            "startLatitude": lat,
            "startLongitude": lon,
            "startTimeLocal": "2026-11-20T18:00:00.0",
        }
        for i, (lat, lon) in enumerate(
            [(40.4168, -3.7038), (40.4170, -3.7040), (42.8460, -2.6850), (42.8462, -2.6852)]
        )
    ]
    api = _FakeActivityApi(activities)
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    return activities


def test_get_popular_areas_ranks_zones_by_activity_count(activity_clusters):
    result = server.get_popular_areas(days=30, max_activities=50)

    assert result["count"] == 2
    assert result["areas"][0]["activities"] == 2
    assert result["areas"][0]["lat"] == pytest.approx(40.4169, abs=1e-4)
    assert result["areas"][1]["lat"] == pytest.approx(42.8461, abs=1e-4)


def test_suggest_routes_from_profile_falls_back_to_inferred_home(activity_clusters, monkeypatch):
    monkeypatch.setattr(server, "_update_user", lambda *a, **k: {})
    monkeypatch.setattr(server, "get_user_profile", lambda: {"home_lat": None, "home_lon": None})
    calls = []

    def fake_suggest_routes(**kwargs):
        calls.append(kwargs)
        return {"routes": []}

    monkeypatch.setattr(server, "suggest_routes", fake_suggest_routes)

    server.suggest_routes_from_profile(distance_km=8)

    assert calls
    assert calls[0]["lat"] == pytest.approx(40.4169, abs=1e-4)
    assert calls[0]["lon"] == pytest.approx(-3.7039, abs=1e-4)
