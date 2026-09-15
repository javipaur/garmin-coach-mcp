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

    # Simulamos un hilo zombi: retenido desde hace muchísimo tiempo. Los tests
    # se ejecutan sin usuario autenticado, así que la clave del lock es "global".
    with server._LOCK_STATE_LOCK:
        server._LOCK_STATES["fetch"]["global"]["since"] = time.monotonic() - 10_000_000

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


def test_distinct_users_do_not_block_each_other():
    """Bug real: un único FETCH_LOCK global hacía que dos usuarios distintos se
    bloquearan entre sí ("El servidor está ocupado con otra consulta a Garmin").
    Con locks por usuario, A puede consultar mientras B tiene el suyo cogido."""
    result: dict[str, object] = {}
    a_started = threading.Event()
    b_released = threading.Event()

    user_a = {"id": "aaaa"}
    user_b = {"id": "bbbb"}

    def _run_as(user, fn, *args, **kwargs):
        # _fetch_lock resuelve el usuario vía current_user (ContextVar).
        tok = server.current_user.set(user)
        try:
            fn(*args, **kwargs)
        finally:
            server.current_user.reset(tok)

    def holder_a():
        def _body():
            with server._fetch_lock(timeout=5):
                a_started.set()
                # A aguanta el lock durante 2s; B debería poder entrar igualmente.
                time.sleep(2)
        _run_as(user_a, _body)

    def contender_b():
        def _body():
            try:
                with server._fetch_lock(timeout=5):
                    result["b_got"] = True
            except server._FetchLockTimeoutError:
                result["b_busy"] = True
            b_released.set()
        _run_as(user_b, _body)

    ta = threading.Thread(target=holder_a, daemon=True)
    ta.start()
    assert a_started.wait(2)

    tb = threading.Thread(target=contender_b, daemon=True)
    tb.start()
    tb.join(4)

    assert result.get("b_got") is True, f"B fue bloqueado por A: {result}"
    assert result.get("b_busy") is None
    assert b_released.is_set()
    ta.join(3)


def test_same_user_calls_serialize():
    """Dos tool calls del MISMO usuario sí deben serializarse (comparten sesión)."""
    result: dict[str, object] = {}
    started = threading.Event()
    user_a = {"id": "cccc"}

    def _run_as(user, fn):
        tok = server.current_user.set(user)
        try:
            fn()
        finally:
            server.current_user.reset(tok)

    def holder():
        def _body():
            with server._fetch_lock(timeout=5):
                started.set()
                time.sleep(2)
        _run_as(user_a, _body)

    def contender():
        def _body():
            try:
                with server._fetch_lock(timeout=0.4):
                    result["got"] = True
            except server._FetchLockTimeoutError:
                result["busy"] = True
        _run_as(user_a, _body)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert started.wait(2)

    c = threading.Thread(target=contender, daemon=True)
    c.start()
    c.join(3)
    t.join(3)

    assert result.get("busy") is True, f"mismo usuario no debía entrar: {result}"
    assert result.get("got") is None
