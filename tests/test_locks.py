"""Tests de contención y autocuración de los locks FETCH_LOCK/REFRESH_LOCK.

Cubren el bug de "servidor saturado": un hilo zombi que retiene FETCH_LOCK sin
soltarlo deja el servidor en "busy" eterno y todas las tool calls fallan con
"El servidor está ocupado con otra consulta a Garmin". También cubren que la
rotación del lock (mecanismo de autocuración) no rompa a los holders legítimos
con `RuntimeError: release unlocked lock`.
"""

import threading
import time

import pytest

import server


@pytest.fixture(autouse=True)
def _clean_lock_state():
    """Deja los locks y su estado aislados entre tests."""
    before = (server.FETCH_LOCK, server.REFRESH_LOCK)
    try:
        yield
    finally:
        server._rotate_fetch_lock()
        assert server.FETCH_LOCK is not before[0] or not before[0].locked()
        assert server.REFRESH_LOCK is not before[1] or not before[1].locked()


def test_fetch_lock_fails_fast_with_busy_error_when_lock_held():
    result: dict[str, object] = {}
    started = threading.Event()

    def holder():
        with server._fetch_lock(timeout=5):
            started.set()
            time.sleep(3)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert started.wait(2)

    def contender():
        try:
            with server._fetch_lock(timeout=0.2):
                result["got"] = True
        except server._FetchLockTimeoutError:
            result["busy"] = True

    c = threading.Thread(target=contender, daemon=True)
    c.start()
    c.join(3)
    t.join(3)

    assert result.get("got") is None
    assert result.get("busy") is True
    assert not c.is_alive()


def test_defensive_release_allows_rotation_without_runtime_error():
    errors: list[Exception] = []
    entered = threading.Event()
    exiting = threading.Event()

    def holder():
        try:
            with server._fetch_lock(timeout=5):
                entered.set()
                if not exiting.wait(5):
                    return
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert entered.wait(2)
    assert server.FETCH_LOCK.locked()

    server._rotate_fetch_lock()
    assert not server.FETCH_LOCK.locked(), "tras rotar, el nuevo lock debe estar libre"

    exiting.set()
    t.join(3)

    assert errors == [], f"la rotación rompió al holder: {errors}"
    with server._fetch_lock(timeout=2):
        assert server.FETCH_LOCK.locked()


def test_watchdog_rotates_stale_fetch_lock_even_with_cache_ok():
    entered = threading.Event()
    releases = []
    previous_lock = server.FETCH_LOCK

    def holder():
        try:
            with server._fetch_lock(timeout=5):
                entered.set()
                time.sleep(2)
        except RuntimeError as exc:
            releases.append(exc)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert entered.wait(2)

    # Simulamos un hilo zombi: retenido desde hace muchísimo tiempo.
    with server._LOCK_STATE_LOCK:
        server._LOCK_STATES["fetch"]["since"] = time.monotonic() - 10_000_000

    with server.CACHE_LOCK:
        server.CACHE["status"] = "ok"

    assert server._lock_watchdog_tick() is True
    assert server.FETCH_LOCK is not previous_lock
    assert not server.FETCH_LOCK.locked(), "el nuevo lock debe quedar libre tras rotar"
    assert server._LOCK_STATES["fetch"] == {}
    with server.CACHE_LOCK:
        assert server.CACHE.get("last_error"), "el watchdog debe dejar registro del fiasco"

    t.join(3)
    assert releases == [], f"el holder legítimo no debe romperse: {releases}"


def test_watchdog_does_not_rotate_fresh_lock():
    entered = threading.Event()

    def holder():
        with server._fetch_lock(timeout=5):
            entered.set()
            time.sleep(0.5)

    previous_lock = server.FETCH_LOCK
    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert entered.wait(2)

    assert server._lock_watchdog_tick() is False
    assert server.FETCH_LOCK is previous_lock
    t.join(2)


def test_lock_holder_info_reports_held_details():
    entered = threading.Event()

    def holder():
        with server._fetch_lock(timeout=5):
            entered.set()
            time.sleep(2)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert entered.wait(2)

    info = server._lock_holder_info("fetch")
    assert info["locked"] is True
    assert isinstance(info["held_seconds"], (int, float))
    assert info["held_seconds"] >= 0
    assert info["thread"]
    t.join(3)

    info_released = server._lock_holder_info("fetch")
    assert info_released["locked"] is False
