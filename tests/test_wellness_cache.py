import threading
import time

import pytest

import config
import server


class FakeGarmin:
    def get_user_summary(self, day=None):
        return {"totalSteps": 12000, "totalDistanceMeters": 9500}

    def get_stats(self, day=None):
        return {"totalSteps": 12000}

    def get_heart_rates(self, day=None):
        return {"restingHeartRate": 52}

    def get_rhr_day(self, day=None):
        return {"restingHeartRate": 52}

    def get_sleep_data(self, day=None):
        return {
            "dailySleepDTO": {
                "calendarDate": day or "2026-09-15",
                "sleepTimeSeconds": 7 * 3600,
            },
            "calendarDate": day or "2026-09-15",
        }

    def get_stress_data(self, day=None):
        return {"stressQualifier": "LOW", "avgStressLevel": 22}

    def get_body_battery(self, day=None):
        return {"bodyBatteryValuesArray": [{"bodyBatteryValue": 65}]}

    def get_hrv_data(self, day=None):
        return {"hrvSummary": {"lastNight": 42}}

    def get_max_metrics(self, day=None):
        return {"vo2Max": 55}

    def get_training_readiness(self, day=None):
        return {"score": 80}

    def get_training_status(self, day=None):
        return {"trainingLoad": {"daily": 300}}

    def get_activities(self, offset=0, limit=1):
        return []

    def get_primary_device_info(self, day=None):
        return {}


class FakeCollector:
    """Sustituye _collect_day_snapshot contando las invocaciones."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.raw_sources = {
            "summary_raw": {"totalSteps": 12000},
            "heart_raw": {"restingHeartRate": 52},
            "sleep_raw": {
                "dailySleepDTO": {"sleepTimeSeconds": 7 * 3600},
            },
            "stress_raw": {"stressQualifier": "LOW"},
            "body_battery_raw": {"bodyBatteryValuesArray": [{"bodyBatteryValue": 65}]},
            "hrv_raw": {"hrvSummary": {"lastNight": 42}},
            "max_metrics_raw": {"vo2Max": 55},
            "training_readiness_raw": {"score": 80},
            "training_status_raw": {"trainingLoad": {"daily": 300}},
        }

    def __call__(self, target_date, include_recent_activities=False, _lock_fn=None):
        with self.lock:
            self.calls.append(target_date)
            n = len(self.calls)
        if n == 1:
            time.sleep(0.2)
        self.ready.set()
        def _fake_lock():
            yield
        return {
            "date": target_date,
            "metrics": {},
            "raw_sources": dict(self.raw_sources),
        }


@pytest.fixture(autouse=True)
def _wipe_user_cache(monkeypatch):
    monkeypatch.setattr(server, "_USER_SNAPSHOT_CACHE", {})
    monkeypatch.setattr(server, "_USER_SNAPSHOT_BUILDING", set())
    user = {"id": "u-test", "display_name": "Test"}
    monkeypatch.setattr(server, "_get_auth_user", lambda: user)
    yield


def _configure_collector(monkeypatch, collector):
    monkeypatch.setattr(server, "_collect_day_snapshot", collector)


def test_same_user_parallel_calls_coalesce(monkeypatch):
    collector = FakeCollector()
    _configure_collector(monkeypatch, collector)

    results = []
    errors = []

    def worker():
        try:
            snap = server._ensure_user_snapshot("2026-09-15")
            results.append(snap["raw_sources"].get("hrv_raw"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(collector.calls) == 1
    assert all(r.get("hrvSummary", {}).get("lastNight") == 42 for r in results)


def test_same_user_second_calls_hit_cache(monkeypatch):
    collector = FakeCollector()
    _configure_collector(monkeypatch, collector)

    first = server._ensure_user_snapshot("2026-09-15")
    second = server._ensure_user_snapshot("2026-09-15")

    assert len(collector.calls) == 1
    assert first["raw_sources"]["hrv_raw"] == second["raw_sources"]["hrv_raw"]


def test_different_dates_do_not_share_cache(monkeypatch):
    collector = FakeCollector()
    _configure_collector(monkeypatch, collector)

    server._ensure_user_snapshot("2026-09-15")
    server._ensure_user_snapshot("2026-09-16")

    assert len(collector.calls) == 2


def test_different_users_do_not_share_cache(monkeypatch):
    collector = FakeCollector()
    _configure_collector(monkeypatch, collector)

    server._ensure_user_snapshot("2026-09-15")

    user_b = {"id": "u-other", "display_name": "Otro"}
    monkeypatch.setattr(server, "_get_auth_user", lambda: user_b)
    server._ensure_user_snapshot("2026-09-15")

    assert len(collector.calls) == 2


def test_stale_cache_refreshes(monkeypatch):
    collector = FakeCollector()
    _configure_collector(monkeypatch, collector)

    server._ensure_user_snapshot("2026-09-15")
    with server._USER_SNAPSHOT_GUARD:
        key = server._user_snapshot_key("u-test", "2026-09-15")
        server._USER_SNAPSHOT_CACHE[key]["fetched_at"] = (
            time.monotonic() - config.WELLNESS_CACHE_TTL_SECONDS - 1
        )

    server._ensure_user_snapshot("2026-09-15")

    assert len(collector.calls) == 2


def test_failed_builder_causes_retry(monkeypatch):
    calls = []

    def flaky_collector(target_date, include_recent_activities=False, _lock_fn=None):
        if len(calls) == 0:
            calls.append("fail")
            raise RuntimeError("Garmin caído")
        calls.append("ok")
        return {
            "date": target_date,
            "metrics": {},
            "raw_sources": {
                "hrv_raw": {"hrvSummary": {"lastNight": 42}},
                "summary_raw": {"totalSteps": 12000},
                "heart_raw": {"restingHeartRate": 52},
                "sleep_raw": {},
                "stress_raw": {},
                "body_battery_raw": {},
                "max_metrics_raw": {},
                "training_readiness_raw": {},
                "training_status_raw": {},
            },
        }

    monkeypatch.setattr(server, "_collect_day_snapshot", flaky_collector)

    results = []
    errors = []

    def worker():
        try:
            snap = server._ensure_user_snapshot("2026-09-15")
            results.append(snap["raw_sources"]["hrv_raw"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls == ["fail", "ok"]
    # El hilo constructor que recibió el error real de Garmin lo propaga a su
    # llamador; el resto espera y reintenta con éxito.
    assert len(errors) == 1
    assert len(results) == 3


def test_tools_serve_from_cached_snapshot(monkeypatch):
    collector = FakeCollector()
    _configure_collector(monkeypatch, collector)

    out = server.get_hrv_data(target_date="2026-09-15")
    assert out["date"] == "2026-09-15"
    assert out["hrv_data"]["hrvSummary"]["lastNight"] == 42

    out = server.get_sleep_data(target_date="2026-09-15")
    assert out["date"] == "2026-09-15"
    assert out["sleep_data"]["dailySleepDTO"]["sleepTimeSeconds"] == 7 * 3600

    out = server.get_max_metrics(target_date="2026-09-15")
    assert out["max_metrics"]["vo2Max"] == 55

    out = server.get_rhr_day(target_date="2026-09-15")
    assert out["rhr"] == 52

    out = server.get_user_summary(target_date="2026-09-15")
    assert out["user_summary"]["totalSteps"] == 12000

    out = server.get_stats(target_date="2026-09-15")
    assert out["stats"]["totalSteps"] == 12000

    out = server.get_body_battery(target_date="2026-09-15")
    assert out["body_battery"]["bodyBatteryValuesArray"][0]["bodyBatteryValue"] == 65

    out = server.get_heart_rates(target_date="2026-09-15")
    assert out["heart_rates"]["restingHeartRate"] == 52

    out = server.get_training_status(target_date="2026-09-15")
    assert out["training_status"]["trainingLoad"]["daily"] == 300

    out = server.get_training_readiness(target_date="2026-09-15")
    assert out["training_readiness"]["score"] == 80

    out = server.get_stress_data(target_date="2026-09-15")
    # la tool registrada con @mcp.tool pasa por _translate_garmin (ES), luego "LOW" -> "Bajo"
    assert out["stress_data"]["stressQualifier"] == "Bajo"

    assert len(collector.calls) == 1


def test_cache_prunes_expired_entries(monkeypatch):
    collector = FakeCollector()
    _configure_collector(monkeypatch, collector)

    server._ensure_user_snapshot("2026-09-15")
    server._ensure_user_snapshot("2026-09-16")
    with server._USER_SNAPSHOT_GUARD:
        dirty = server._user_snapshot_key("u-test", "2026-09-15")
        server._USER_SNAPSHOT_CACHE[dirty]["fetched_at"] = (
            time.monotonic() - config.WELLNESS_CACHE_TTL_SECONDS - 10
        )
    assert len(server._USER_SNAPSHOT_CACHE) == 2

    server._ensure_user_snapshot("2026-09-17")
    keys = list(server._USER_SNAPSHOT_CACHE.keys())
    assert len(keys) == 2
    # La entrada expirada se purgó; quedan 16 y 17.
    assert server._user_snapshot_key("u-test", "2026-09-15") not in keys
    assert server._user_snapshot_key("u-test", "2026-09-16") in keys
    assert server._user_snapshot_key("u-test", "2026-09-17") in keys
