import pytest
from starlette.requests import Request

import server


def test_polyline_round_trip_keeps_coordinates_within_gps_precision():
    points = [(40.416775, -3.70379), (40.420000, -3.710000), (40.0, -3.0)]

    encoded = server._encode_polyline(points)
    decoded = server._decode_polyline(encoded)

    assert encoded
    assert len(decoded) == len(points)
    for (lat, lon), (dlat, dlon) in zip(points, decoded, strict=True):
        assert dlat == pytest.approx(lat, abs=1e-5)
        assert dlon == pytest.approx(lon, abs=1e-5)


def test_get_route_map_url_builds_shareable_and_external_links(monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://garmin.example.com")
    route = {
        "name": "Ruta 10K",
        "points": [{"lat": 40.416775, "lon": -3.70379}, {"lat": 40.42, "lon": -3.71}],
    }

    result = server.get_route_map_url(route)

    assert result["viewer_url"].startswith("https://garmin.example.com/rutas#d=")
    assert "Ruta%2010K" in result["viewer_url"]
    assert result["external_url"].startswith("https://www.openstreetmap.org/")
    assert result["point_count"] == 2


def test_get_route_map_url_uses_http_request_when_public_url_is_unset(monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
        "headers": [
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"garmin.javierpalacio.es"),
        ],
    }
    monkeypatch.setattr(server, "get_http_request", lambda: Request(scope))
    route = {
        "name": "Ruta 8K",
        "points": [{"lat": 42.84, "lon": -2.68}, {"lat": 42.85, "lon": -2.69}],
    }

    result = server.get_route_map_url(route)

    assert result["viewer_url"].startswith("https://garmin.javierpalacio.es/rutas#d=")


def test_get_route_map_url_rejects_empty_routes():
    with pytest.raises(RuntimeError, match="no tiene puntos GPS"):
        server.get_route_map_url({"points": []})


def test_route_map_html_contains_leaflet_viewer_and_hash_decoder():
    html = server._render_route_map_html()

    assert "leaflet" in html.lower()
    assert "decodePolyline" in html
    assert "location.hash" in html
    assert 'id="map"' in html


def test_route_map_route_is_registered():
    paths = [getattr(r, "path", None) for r in server.mcp._get_additional_http_routes()]

    assert "/rutas" in paths
