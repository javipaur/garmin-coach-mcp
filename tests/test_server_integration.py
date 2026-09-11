import asyncio
import json
import threading
import time

import httpx
import pytest
import uvicorn

import server
import snapshot


class FakeGarmin:
    def get_user_summary(self, day=None):
        return {"totalSteps": 12000, "totalDistanceMeters": 9500}

    def get_stats(self, day=None):
        return {"totalSteps": 12000}

    def get_heart_rates(self, day=None):
        return {}

    def get_rhr_day(self, day=None):
        return {}

    def get_sleep_data(self, day=None):
        return {
            "dailySleepDTO": {
                "calendarDate": day or "2026-09-11",
                "sleepTimeSeconds": 7 * 3600,
                "sleepScores": {"wellness": {"overall": 80}},
                "sleepLevels": {"summary": {"deep": {"seconds": 3000}, "rem": {"seconds": 4000}, "light": {"seconds": 12000}}},
            },
            "calendarDate": day or "2026-09-11",
        }

    def get_stress_data(self, day=None):
        return {"stressQualifier": "LOW", "avgStressLevel": 22}

    def get_body_battery(self, day=None):
        return {}

    def get_hrv_data(self, day=None):
        return {}

    def get_max_metrics(self, day=None):
        return {}

    def get_training_readiness(self, day=None):
        return {"score": 70, "readinessLevelEnum": "HIGH"}

    def get_training_status(self, day=None):
        return {}


@pytest.fixture(scope="session")
def live_server() -> str:
    app = server.mcp.http_app(stateless_http=True)
    app.add_middleware(server._RateLimitMiddleware, max_requests=3, window_seconds=60)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()

    port = None
    deadline = time.time() + 15
    while time.time() < deadline:
        if srv.started and srv.servers:
            port = srv.servers[0].sockets[0].getsockname()[1]
            break
        time.sleep(0.05)
    assert port, "uvicorn no arrancó"
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/health", timeout=5).status_code == 200:
                break
        except Exception:
            time.sleep(0.1)
    yield base
    srv.should_exit = True
    thread.join(timeout=10)


def test_health_endpoint(live_server):
    r = httpx.get(live_server + "/health", timeout=5)
    assert r.status_code == 200
    payload = r.json()
    assert payload.get("status") == "ok"
    assert payload.get("mcp_endpoint") == "/mcp"


def test_landing_page(live_server):
    r = httpx.get(live_server + "/", timeout=5)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_mcp_initialize_handshake(live_server):
    r = httpx.post(
        live_server + "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "integration", "version": "1"},
            },
        },
        headers={"Accept": "application/json, text/event-stream"},
        timeout=10,
    )
    assert r.status_code == 200
    assert "data: " in r.text
    data_line = next(line for line in r.text.splitlines() if line.startswith("data: "))
    body = json.loads(data_line[len("data: "):])
    assert body.get("id") == 1
    assert body.get("result", {}).get("serverInfo", {}).get("name")
    assert isinstance(body.get("result", {}).get("capabilities"), dict)


def test_mcp_exposes_more_than_120_tools():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) >= 120


def test_metrics_endpoint(live_server):
    r = httpx.get(live_server + "/metrics", timeout=5)
    assert r.status_code == 200
    text = r.text
    assert "gcmcp_uptime_seconds" in text
    assert "gcmcp_cache_status" in text


def test_rate_limit_on_admin(live_server):
    for _ in range(3):
        r = httpx.get(live_server + "/admin", timeout=5, follow_redirects=True)
        assert r.status_code == 200
    r = httpx.get(live_server + "/admin", timeout=5, follow_redirects=True)
    assert r.status_code == 429


def test_binding_uses_snapshot_pipeline():
    assert server._collect_day_snapshot is snapshot._collect_day_snapshot


def _configure_real_chain_with_fake():
    snapshot.configure(
        FETCH_LOCK=server.FETCH_LOCK,
        ACTIVITY_LIMIT=server.ACTIVITY_LIMIT,
        _get_api=lambda: FakeGarmin(),
        _optional_call_first=server._optional_call_first,
        _collect_extra_raw=server._collect_extra_raw,
        _resting_hr=server._resting_hr,
        _extract_vo2=server._extract_vo2,
        _sleep_metrics=server._sleep_metrics,
        _stress_metrics=server._stress_metrics,
        _body_battery_metrics=server._body_battery_metrics,
        _hrv_metrics=server._hrv_metrics,
        _training_readiness_metrics=server._training_readiness_metrics,
        _normalize_activity=server._normalize_activity,
        _select_training_readiness_entry=server._select_training_readiness_entry,
        _attach_frontend_view_to_snapshot=server._attach_frontend_view_to_snapshot,
        _get_hrv_debug_last=lambda: None,
    )


def test_get_day_snapshot_produces_full_snapshot():
    _configure_real_chain_with_fake()
    result = server._collect_day_snapshot("2026-09-11", include_recent_activities=False)
    assert isinstance(result, dict)
    assert result["date"].isoformat() == "2026-09-11"
    assert "metrics" in result
    assert "raw_sources" in result
    assert "source_errors" in result
    metrics = result["metrics"]
    assert metrics["steps"] == 12000


def test_parse_training_pdf_unauthorized(monkeypatch):
    monkeypatch.setattr(server, "_get_auth_user", lambda: None)
    out = server.parse_training_pdf(
        pdf_base64="QUJD",
        start_date="2026-09-11",
    )
    assert out.get("error") == "No autenticado"


def test_parse_training_pdf_full_flow(monkeypatch):
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "u1", "name": "Test"})
    monkeypatch.setattr(
        server,
        "extract_pdf_text",
        lambda pdf_base64, filename=None: {
            "ok": True,
            "full_text": "Semana 1:\nLunes 5km suave Z2\nMiércoles 3x800m 400m rec\nViernes 8km\n",
        },
    )
    monkeypatch.setattr(
        server,
        "create_training_plan",
        lambda plan_name, start_date, sessions, auto_push_to_device=False: {
            "ok": True,
            "plan_name": plan_name,
            "created": len(sessions),
        },
    )
    out = server.parse_training_pdf(
        pdf_base64="QUJD",
        start_date="2026-09-11",
    )
    assert out.get("error") is None
    assert out.get("source") == "pdf"
    assert out["created"] == 3


def test_suggest_routes_invokes_generate(monkeypatch):
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "u1"})
    monkeypatch.setattr(
        server,
        "_generate_loop_route",
        lambda *a, **k: [{"distance_km": 5.0, "point_count": 3, "points": []}],
    )
    out = server.suggest_routes(distance_km=5, lat=40.4167, lon=-3.7033)
    assert isinstance(out, dict)
    assert out["count"] == 1
    assert out["routes"][0]["distance_km"] == 5.0