"""Tests del endurecimiento: snapshot en disco, versión, edad/stale de caché y
heartbeat de tokens Garmin.

Todos son offline: no tocan la red. Aíslan la CACHE global entre tests y las
rutas de ficheros de tokens mediante monkeypatch.
"""

import copy
import json
import time
from pathlib import Path

import pytest

import server


@pytest.fixture(autouse=True)
def _restore_cache():
    """Deja CACHE exactamente como estaba antes de cada test."""
    keys = list(server.CACHE.keys())
    before = {k: copy.deepcopy(server.CACHE[k]) for k in keys}
    yield
    with server.CACHE_LOCK:
        server.CACHE.clear()
        server.CACHE.update(before)


def _write_tokens(path: Path, expires_at: float | None = None, expires_in: int | None = None):
    oauth2 = {"access_token": "tok", "token_type": "Bearer", "scope": ""}
    if expires_at is not None:
        oauth2["expires_at"] = expires_at
    if expires_in is not None:
        oauth2["expires_in"] = expires_in
    payload = {"oauth1_tokens": {"consumer_key": "k"}, "oauth2_tokens": oauth2}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_persist_and_load_roundtrip(tmp_path):
    target = tmp_path / "last_snapshot.json"
    snap = {"metrics": {"pasos": 123}, "fetched_at": "2026-09-14T10:00:00.000Z"}
    assert server._persist_cache_snapshot(snap, path=target) is True
    loaded = server._load_persisted_cache_snapshot(path=target)
    assert loaded == snap


def test_persist_ignores_non_dict(tmp_path):
    assert server._persist_cache_snapshot(None, path=tmp_path / "x.json") is False
    assert server._persist_cache_snapshot("nope", path=tmp_path / "x.json") is False


def test_bootstrap_restores_cache_from_disk(tmp_path, monkeypatch):
    snap = {"metrics": {"pasos": 999}, "fetched_at": "2026-09-14T09:00:00.000Z"}
    target = tmp_path / "last_snapshot.json"
    server._persist_cache_snapshot(snap, path=target)
    monkeypatch.setattr(server, "SNAPSHOT_PERSIST_FILE", target)
    with server.CACHE_LOCK:
        server.CACHE["status"] = "starting"
        server.CACHE["snapshot"] = None
        server.CACHE["last_refresh"] = None
    assert server._bootstrap_cache_from_disk() is True
    with server.CACHE_LOCK:
        assert server.CACHE["status"] == "ok"
        assert server.CACHE["snapshot"].get("metrics", {}).get("pasos") == 999
        assert server.CACHE["last_refresh"] == "2026-09-14T09:00:00.000Z"


def test_bootstrap_without_file_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SNAPSHOT_PERSIST_FILE", tmp_path / "no-existe.json")
    assert server._bootstrap_cache_from_disk() is False


def test_cache_age_and_stale():
    assert server._cache_age_seconds() is None
    assert server._cache_is_stale() is False
    with server.CACHE_LOCK:
        server.CACHE["last_refresh"] = server._now_iso()  # recién refrescado
    assert server._cache_age_seconds() is not None
    assert server._cache_is_stale() is False
    with server.CACHE_LOCK:
        server.CACHE["last_refresh"] = "2020-01-01T00:00:00+00:00"  # hace muchísimo
    assert server._cache_is_stale() is True


def test_git_revision_is_nonempty():
    rev = server._git_revision()
    assert isinstance(rev, str)
    assert rev.strip() != ""


def test_read_token_expires_at_favours_expires_at(tmp_path):
    token_file = tmp_path / "users" / "u1" / "garmin_tokens.json"
    _write_tokens(token_file, expires_at=1111.0, expires_in=2222)
    assert server._read_token_expires_at(token_file) == 1111.0


def test_read_token_expires_at_falls_back_to_expires_in(tmp_path):
    token_file = tmp_path / "garmin_tokens.json"
    _write_tokens(token_file, expires_in=3600)
    val = server._read_token_expires_at(token_file)
    assert val is not None
    assert val > time.time()


def test_read_token_expires_at_invalid_file(tmp_path):
    bogus = tmp_path / "garmin_tokens.json"
    bogus.write_text("no es json", encoding="utf-8")
    assert server._read_token_expires_at(bogus) is None


def test_heartbeat_tick_refreshes_only_tokens_near_expiry(tmp_path, monkeypatch):
    near = tmp_path / "users" / "u1" / "garmin_tokens.json"
    far = tmp_path / "users" / "u2" / "garmin_tokens.json"
    _write_tokens(near, expires_at=time.time() - 60)  # caduca ya
    _write_tokens(far, expires_at=time.time() + 3600 * 4)  # lejos de caducar

    monkeypatch.setattr(server, "TOKEN_FILE", tmp_path / "primary.json")
    monkeypatch.setattr(server, "USERS_DB_DIR", tmp_path / "users")
    monkeypatch.setattr(server, "TOKEN_REFRESH_LEAD_SECONDS", 60 * 60)
    calls: list[str] = []

    def _fake_refresh(path):
        calls.append(path.parent.name)
        return True

    monkeypatch.setattr(server, "_refresh_token_file", _fake_refresh)

    refreshed = server._token_heartbeat_tick()
    # _token_heartbeat_tick guarda el nombre del fichero (path.name).
    assert refreshed == ["garmin_tokens.json"]
    # _fake_refresh registra el directorio del usuario (path.parent.name).
    assert calls == ["u1"]


def test_heartbeat_skips_missing_token_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "TOKEN_FILE", tmp_path / "sin-fichero.json")
    monkeypatch.setattr(server, "USERS_DB_DIR", tmp_path / "users-inexistente")
    monkeypatch.setattr(server, "TOKEN_REFRESH_LEAD_SECONDS", 3600)
    assert server._token_heartbeat_tick() == []
