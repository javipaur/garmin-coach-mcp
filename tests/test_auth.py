import auth
from auth import (
    _create_user,
    _delete_user,
    _get_user_by_api_key,
    _get_user_by_id,
    _hash_api_key,
    _update_user,
)


def _use_tmp_db(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "USERS_DB_DIR", tmp_path)
    monkeypatch.setattr(auth, "USERS_DB_FILE", tmp_path / "users.json")


def test_generate_api_key_format():
    key = auth._generate_api_key()
    assert key.startswith("gcmcp_")
    assert len(key) == 54


def test_hash_api_key_deterministic():
    assert _hash_api_key("abc") == _hash_api_key("abc")
    assert _hash_api_key("abc") != _hash_api_key("abd")


def test_create_and_get_user(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    user = _create_user("Pepe", "pepe@example.com")
    assert user["display_name"] == "Pepe"
    assert _get_user_by_id(user["id"])["api_key"] == user["api_key"]
    assert _get_user_by_api_key(user["api_key"])["display_name"] == "Pepe"


def test_get_user_by_api_key_wrong_key(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    user = _create_user("Ana")
    assert _get_user_by_api_key("wrong") is None
    assert user["api_key"] is not None


def test_update_user(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    user = _create_user("Luis")
    updated = _update_user(user["id"], display_name="Luis 2", home_lat=40.4)
    assert updated["display_name"] == "Luis 2"
    assert updated["home_lat"] == 40.4
    assert _update_user("nonexistent", display_name="x") is None


def test_delete_user(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    user = _create_user("Rosa")
    assert _delete_user(user["id"]) is True
    assert _get_user_by_id(user["id"]) is None
    assert _delete_user(user["id"]) is False


def test_user_isolation(monkeypatch, tmp_path):
    _use_tmp_db(monkeypatch, tmp_path)
    user_a = _create_user("A")
    user_b = _create_user("B")
    assert _get_user_by_api_key(user_a["api_key"])["id"] == user_a["id"]
    assert _get_user_by_api_key(user_b["api_key"])["id"] == user_b["id"]
