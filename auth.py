from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from config import ADMIN_TOKEN, AUTH_COOKIE_MAX_AGE_SECONDS, USERS_DB_DIR, USERS_DB_FILE

USERS_DB_LOCK = threading.Lock()

current_user: ContextVar[dict[str, Any] | None] = ContextVar("current_user", default=None)


def _get_auth_user() -> dict[str, Any] | None:
    return current_user.get()


def _generate_id(length: int = 8) -> str:
    return secrets.token_hex(length // 2)


def _generate_api_key() -> str:
    return f"gcmcp_{secrets.token_hex(24)}"


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _load_users_db() -> dict[str, Any]:
    with USERS_DB_LOCK:
        if USERS_DB_FILE.exists():
            try:
                return json.loads(USERS_DB_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"users": {}}


def _save_users_db(db: dict[str, Any]) -> None:
    USERS_DB_DIR.mkdir(parents=True, exist_ok=True)
    with USERS_DB_LOCK:
        USERS_DB_FILE.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_user_by_api_key(api_key: str) -> dict[str, Any] | None:
    db = _load_users_db()
    for user in db.get("users", {}).values():
        stored_key = user.get("api_key", "")
        if secrets.compare_digest(stored_key, api_key):
            return user
    return None


def _get_user_by_id(user_id: str) -> dict[str, Any] | None:
    db = _load_users_db()
    return db.get("users", {}).get(user_id)


def _create_user(display_name: str, garmin_email: str = "") -> dict[str, Any]:
    user_id = _generate_id()
    api_key = _generate_api_key()
    user_dir = USERS_DB_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    user = {
        "id": user_id,
        "api_key": api_key,
        "display_name": display_name,
        "garmin_email": garmin_email,
        "created_at": datetime.now(UTC).isoformat(),
        "home_lat": None,
        "home_lon": None,
        "home_name": "",
    }
    db = _load_users_db()
    db.setdefault("users", {})[user_id] = user
    _save_users_db(db)
    return user


def _delete_user(user_id: str) -> bool:
    db = _load_users_db()
    if user_id in db.get("users", {}):
        del db["users"][user_id]
        _save_users_db(db)
        user_dir = USERS_DB_DIR / user_id
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)
        return True
    return False


def _update_user(user_id: str, **fields: Any) -> dict[str, Any] | None:
    db = _load_users_db()
    user = db.get("users", {}).get(user_id)
    if not user:
        return None
    for k, v in fields.items():
        if k in ("display_name", "garmin_email", "home_lat", "home_lon", "home_name"):
            user[k] = v
    _save_users_db(db)
    return user


def _user_token_dir(user_id: str) -> Path:
    return USERS_DB_DIR / user_id


def _user_token_file(user_id: str) -> Path:
    return _user_token_dir(user_id) / "garmin_tokens.json"


def _seed_user_token_file(user_id: str, token_dir: Path) -> None:
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / "garmin_tokens.json"
    if token_file.exists():
        return
    user = _get_user_by_id(user_id)
    if user and user.get("garmin_tokens_json"):
        parsed = _json_loads_maybe_base64(user["garmin_tokens_json"])
        token_file.write_text(json.dumps(parsed), encoding="utf-8")
        return
    legacy = os.getenv("GARMIN_TOKENS_JSON", "").strip()
    if legacy and not token_file.exists():
        parsed = _json_loads_maybe_base64(legacy)
        token_file.write_text(json.dumps(parsed), encoding="utf-8")


def _json_loads_maybe_base64(raw: str) -> dict[str, Any]:
    import base64
    raw = raw.strip()
    if not raw:
        raise RuntimeError("GARMIN_TOKENS_JSON está vacío")
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError("GARMIN_TOKENS_JSON no contiene un objeto JSON válido")
        return parsed
    except json.JSONDecodeError:
        pass
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        parsed = json.loads(decoded)
        if not isinstance(parsed, dict):
            raise RuntimeError("El base64 no contiene un objeto JSON válido")
        return parsed
    except Exception as exc:
        raise RuntimeError(
            "GARMIN_TOKENS_JSON no es JSON válido ni base64 de JSON válido"
        ) from exc


# --- Admin token management ---

def _admin_token_file() -> Path:
    from config import TOKEN_DIR
    return TOKEN_DIR / "admin_token"


def _current_admin_token() -> str:
    try:
        f = _admin_token_file()
        if f.exists():
            v = f.read_text(encoding="utf-8").strip()
            if v:
                return v
    except Exception:
        pass
    return ADMIN_TOKEN


def _write_admin_token_file(value: str) -> None:
    f = _admin_token_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(value, encoding="utf-8")


def _login_admin_ok(request: Any) -> bool:
    admin = _current_admin_token()
    if not admin:
        return True
    cookie = request.cookies.get("admin_token", "")
    qp = request.query_params.get("token", "")
    if cookie and secrets.compare_digest(cookie, admin):
        return True
    return bool(qp and secrets.compare_digest(qp, admin))


def _login_active_token(request: Any) -> str:
    admin = _current_admin_token()
    qp = request.query_params.get("token", "")
    if admin and qp and secrets.compare_digest(qp, admin):
        return admin
    return ""


def _set_auth_cookie(response: Any, token: str, request: Any) -> None:
    response.set_cookie(
        "admin_token", token, httponly=True, samesite="strict",
        max_age=AUTH_COOKIE_MAX_AGE_SECONDS, secure=_request_is_https(request),
    )


def _request_user(request: Any) -> dict[str, Any] | None:
    api_key = request.cookies.get("user_api_key", "").strip()
    if not api_key:
        return None
    return _get_user_by_api_key(api_key)


def _set_user_cookie(response: Any, api_key: str, request: Any) -> None:
    response.set_cookie(
        "user_api_key", api_key, httponly=True, samesite="strict",
        max_age=AUTH_COOKIE_MAX_AGE_SECONDS, secure=_request_is_https(request),
    )


def _request_is_https(request: Any) -> bool:
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.lower() == "https"


# --- Login sessions ---

LOGIN_SESSION_TTL_SECONDS = 600
LOGIN_MFA_TIMEOUT_SECONDS = 300

_LOGIN_SESSIONS: dict[str, dict[str, Any]] = {}
_LOGIN_SESSIONS_LOCK = threading.Lock()


def _login_cleanup_expired_sessions() -> None:
    now = time.time()
    with _LOGIN_SESSIONS_LOCK:
        stale = [
            sid
            for sid, s in _LOGIN_SESSIONS.items()
            if now - s.get("created_at", now) > LOGIN_SESSION_TTL_SECONDS
        ]
        for sid in stale:
            _LOGIN_SESSIONS.pop(sid, None)


def _login_get_session(session_id: str) -> dict[str, Any] | None:
    with _LOGIN_SESSIONS_LOCK:
        return _LOGIN_SESSIONS.get(session_id)


def _login_set_session(session_id: str, data: dict[str, Any]) -> None:
    with _LOGIN_SESSIONS_LOCK:
        _LOGIN_SESSIONS[session_id] = data


def _login_drop_session(session_id: str) -> None:
    with _LOGIN_SESSIONS_LOCK:
        _LOGIN_SESSIONS.pop(session_id, None)


def _find_user_by_email(email: str) -> dict[str, Any] | None:
    db = _load_users_db()
    for user in db.get("users", {}).values():
        if user.get("garmin_email", "").lower() == email.lower():
            return user
    return None
