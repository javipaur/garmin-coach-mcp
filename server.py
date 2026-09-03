from __future__ import annotations

import base64
import functools
import json
import os
import threading
import time
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from contextvars import ContextVar
import contextvars
import hashlib
import secrets
import io
import re
import math


APP_NAME = "Garmin Coach MCP"
CACHE_MINUTES = max(5, int(os.getenv("CACHE_MINUTES", "30")))
ACTIVITY_LIMIT = max(1, min(20, int(os.getenv("ACTIVITY_LIMIT", "8"))))
PORT = int(os.getenv("PORT", "8000"))
APP_TIMEZONE = ZoneInfo(os.getenv("GARMIN_TIMEZONE", "Europe/Madrid"))
RECOVERY_MAX_FRESH_MINUTES = max(15, int(os.getenv("RECOVERY_MAX_FRESH_MINUTES", "360")))
RECOVERY_CROSS_DAY_STALE_MINUTES = max(15, int(os.getenv("RECOVERY_CROSS_DAY_STALE_MINUTES", "180")))
GARMIN_LANGUAGE = os.getenv("GARMIN_LANGUAGE", "es").lower()
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()

# ---------------------------------------------------------------------------
# Multi-user system
# ---------------------------------------------------------------------------
USERS_DB_DIR = Path(os.getenv("USERS_DB_DIR", "/data/users"))
USERS_DB_FILE = USERS_DB_DIR / "users.json"
USERS_DB_LOCK = threading.Lock()

current_user: ContextVar[dict[str, Any] | None] = ContextVar("current_user", default=None)


def _get_auth_user() -> dict[str, Any] | None:
    """Retrieve the current authenticated user from ContextVar."""
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
    for uid, user in db.get("users", {}).items():
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
        "created_at": datetime.utcnow().isoformat() + "Z",
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
        import shutil
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


# Context variable for route generation caching
# Route graph cache is a module-level dict, not ContextVar

# Traducción de enums de la API de Garmin al español de Garmin Connect
_GARMIN_ES: dict[str, str] = {
    # HRV / VFC
    "BALANCED": "Equilibrado",
    "UNBALANCED": "Desequilibrado",
    "LOW": "Bajo",
    "POOR": "Deficiente",
    "NO_STATUS": "Sin estado",

    # Estado de entrenamiento (Training Status)
    "PRODUCTIVE": "Productivo",
    "MAINTAINING": "Manteniendo",
    "RECOVERY": "Recuperación",
    "OVERREACHING": "Sobreentrenamiento",
    "UNPRODUCTIVE": "No productivo",
    "DETRAINING": "Pérdida de forma",
    "PEAKING": "Pico de forma",
    "OVERLOAD": "Sobrecarga",

    # Predisposición para entrenar (Training Readiness)
    "EXCELLENT": "Óptima",
    "GOOD": "Alta",
    "FAIR": "Moderada",
    "BAD": "Baja",
    "VERY_BAD": "Muy baja",

    # Fases de sueño (la API puede devolver mayúsculas o minúsculas)
    "AWAKE": "Despierto",
    "LIGHT": "Ligero",
    "DEEP": "Profundo",
    "REM": "REM",
    "awake": "Despierto",
    "light": "Ligero",
    "deep": "Profundo",
    "rem": "REM",

    # Puntuación de sueño (Sleep Score)
    # GOOD → "Buena" (se comparte con Training Readiness, forma masculina es "Bueno")
    # FAIR → "Regular" (ya definido arriba)
    # POOR → "Deficiente" (ya definido arriba)
    # EXCELLENT → "Excelente" (ya definido arriba)

    # Efecto del entrenamiento (Training Effect)
    "IMPROVING": "Mejorando",
    "HIGHLY_AEROBIC": "Aeróbico intenso",
    "AEROBIC": "Aeróbico",
    "ANAEROBIC": "Anaeróbico",
    "VO2MAX": "Mejora VO2max",
    "ANAEROBIC_CAPACITY": "Capacidad anaeróbica",
    "AEROBIC_BASE": "Base aeróbica",

    # Zonas de intensidad
    "ZONE_1": "Calentamiento",
    "ZONE_2": "Suave",
    "ZONE_3": "Aeróbica",
    "ZONE_4": "Umbral",
    "ZONE_5": "Máximo",

    # Tipos de actividad
    "treadmill_running": "Carrera en cinta",
    "strength_training": "Fuerza",

    # Mensajes Body Battery / feedback UI
    "DAY_STRESSFUL_AND_INACTIVE": "Día estresante e inactivo",
    "SLEEP_TIME_PASSED_STRESSFUL_AND_INACTIVE": "Noche estresante + inactividad",

    # Insights de sueño
    "NEGATIVE_STRENUOUS_EXERCISE": "Ejercicio intenso previo",
    "HARD_EXERCISE_NEG_FAIR_OR_POOR_SLEEP": "Entrenamiento duro + mal sueño",

    # Estados genéricos de nivel / calidad
    "OPTIMAL": "Óptimo",
    "MODERATE": "Moderado",
    "HIGH": "Alto",
    "NORMAL": "Normal",
    "ABOVE_NORMAL": "Por encima de lo normal",
    "BELOW_NORMAL": "Por debajo de lo normal",

    # Tendencias (composición corporal, peso, VO2max…)
    "STABLE": "Estable",
    "INCREASING": "En aumento",
    "DECREASING": "En descenso",
    "IMPROVED": "Mejorado",
    "DECLINED": "Empeorado",
    "UNCHANGED": "Sin cambios",
    "INCREASED": "Aumentado",
    "DECREASED": "Disminuido",

    # Estado de retos / objetivos
    "ACTIVE": "Activo",
    "INACTIVE": "Inactivo",
    "COMPLETED": "Completado",
    "IN_PROGRESS": "En progreso",
    "PENDING": "Pendiente",
    "FAILED": "No completado",
    "AVAILABLE": "Disponible",

    # Sistema de unidades
    "METRIC": "Métrico",
    "STATUTE": "Imperial",
    "MARINE": "Náutico",

    # Perfil / género
    "MALE": "Masculino",
    "FEMALE": "Femenino",

    # SPO2
    "STANDARD": "Estándar",
    "CONTINUOUS": "Continuo",
    "SPOT_CHECK": "Medición puntual",
    "INTERRUPTED": "Interrumpido",
    "HIGH_ALTITUDE": "Altitud elevada",
    "ENABLED": "Activo",
    "DISABLED": "Desactivado",

    # Respiración
    "TACHYPNEA": "Taquipnea",
    "BRADYPNEA": "Bradipnea",

    # Tipos de actividad adicionales
    "running": "Correr",
    "cycling": "Ciclismo",
    "walking": "Caminar",
    "hiking": "Senderismo",
    "swimming": "Natación",
    "trail_running": "Trail running",
    "road_biking": "Ciclismo en carretera",
    "indoor_cycling": "Ciclismo indoor",
    "mountain_biking": "Ciclismo de montaña",
    "virtual_ride": "Ciclismo virtual",
    "open_water_swimming": "Natación en aguas abiertas",
    "pool_swimming": "Natación en piscina",
    "cardio": "Cardio",
    "elliptical": "Elíptica",
    "track_running": "Carrera en pista",
    "multi_sport": "Multideporte",
    "triathlon": "Triatlón",
    "yoga": "Yoga",
    "pilates": "Pilates",
    "tennis": "Tenis",
    "golf": "Golf",
    "rowing": "Remo",
    "cross_country_skiing": "Esquí de fondo",
    "skiing": "Esquí alpino",
    "snowboarding": "Snowboard",
    "basketball": "Baloncesto",
    "football": "Fútbol americano",
    "soccer": "Fútbol",
    "other": "Otro",

    # Workout — tipos de paso
    "WARMUP": "Calentamiento",
    "COOLDOWN": "Vuelta a la calma",
    "INTERVAL": "Intervalo",
    "RECOVERY": "Recuperación",
    "REST": "Descanso",
    "RECOVER": "Recuperación",
    "REPEAT": "Repetición",
    "REPEAT_STEP": "Bloque de repetición",
    "ACTIVE": "Activo",

    # Workout — tipos de objetivo (target)
    "NO_TARGET": "Sin objetivo",
    "OPEN": "Abierto",
    "LAP_BUTTON": "Botón vuelta",
    "HEART_RATE": "Frecuencia cardíaca",
    "POWER": "Potencia",
    "CADENCE": "Cadencia",
    "PACE": "Ritmo",
    "SPEED": "Velocidad",
    "GRADE": "Pendiente",
    "ITERATIONS": "Repeticiones",

    # Workout — tipos de duración
    "TIME": "Tiempo",
    "REPS": "Repeticiones",
    "FIXED_REST": "Descanso fijo",

    # Workout — deportes
    "RUNNING": "Correr",
    "CYCLING": "Ciclismo",
    "SWIMMING": "Natación",
    "FITNESS_EQUIPMENT": "Máquina de fitness",
    "STRENGTH_TRAINING": "Fuerza",
    "CARDIO_TRAINING": "Cardio",
    "WALK": "Caminar",

    # Workout — estado en calendario
    "SCHEDULED": "Planificado",
    "SKIPPED": "Omitido",
    "MISSED": "No realizado",

    # Calendario — tipo de elemento
    "workout": "Entrenamiento",
    "race": "Carrera",
    "note": "Nota",
    "garmincoach": "Garmin Coach",

    # Nutrición — comidas
    "BREAKFAST": "Desayuno",
    "LUNCH": "Almuerzo",
    "DINNER": "Cena",
    "SNACK": "Tentempié",
    "WATER": "Agua",
    "SUPPLEMENT": "Suplemento",
    "ANYTIME": "En cualquier momento",

    # Genéricos
    "UNKNOWN": "Desconocido",
    "NONE": "Sin datos",
    "NO_DATA": "Sin datos",
    "POSITIVE": "Positivo",
    "NEGATIVE": "Negativo",
    "NEUTRAL": "Neutral",
    "ASCENDING": "Ascendente",
    "DESCENDING": "Descendente",
    "WEEKLY": "Semanal",
    "DAILY": "Diario",
    "DISTANCE": "Distancia",
    "DURATION": "Duración",
    "CALORIES": "Calorías",
    "STEPS": "Pasos",
}


def _translate_garmin(obj: Any) -> Any:
    """Traduce recursivamente los enums de Garmin al español de Garmin Connect."""
    if not GARMIN_LANGUAGE.startswith("es"):
        return obj
    if isinstance(obj, dict):
        return {k: _translate_garmin(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_translate_garmin(i) for i in obj]
    if isinstance(obj, str) and obj in _GARMIN_ES:
        return _GARMIN_ES[obj]
    return obj

DATA_ROOT = Path(os.getenv("DATA_DIR", "/data"))
LOCAL_GARMINCONNECT_DIR = Path.home() / ".garminconnect"
LOCAL_DEBUG_TOKEN_DIR = Path.cwd() / ".debug-data" / "garmin"

def _resolve_token_dir() -> Path:
    explicit = os.getenv("GARMIN_TOKEN_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    if LOCAL_GARMINCONNECT_DIR.exists():
        return LOCAL_GARMINCONNECT_DIR

    if DATA_ROOT.exists() and os.access(DATA_ROOT, os.W_OK):
        return DATA_ROOT / "garmin"

    return LOCAL_DEBUG_TOKEN_DIR

TOKEN_DIR = _resolve_token_dir()
TOKEN_FILE = TOKEN_DIR / "garmin_tokens.json"

GARMIN_TOKENS_JSON = os.getenv("GARMIN_TOKENS_JSON", "").strip()
RESET_GARMIN_TOKENS = os.getenv("RESET_GARMIN_TOKENS", "0").lower() in {"1", "true", "yes"}

# Re-login web flow (mobile-friendly wizard at /login)
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
LOGIN_SESSION_TTL_SECONDS = 600
LOGIN_MFA_TIMEOUT_SECONDS = 300
# Auth cookie lifetime. Refreshed on each visit (sliding), so in practice you stay
# logged in as long as you keep opening the panel within this window.
AUTH_COOKIE_MAX_AGE_SECONDS = 31536000  # 365 days

# Track the last time the /mcp endpoint was hit, used by the dashboard to tell
# the user whether an IA has actually connected to this server.
_LAST_MCP_HIT_LOCK = threading.Lock()
_LAST_MCP_HIT: float | None = None
_LAST_MCP_CLIENT: str = ""

_LOGIN_SESSIONS: dict[str, dict[str, Any]] = {}
_LOGIN_SESSIONS_LOCK = threading.Lock()

CACHE_LOCK = threading.Lock()
FETCH_LOCK = threading.Lock()

CACHE: dict[str, Any] = {
    "status": "starting",
    "last_refresh": None,
    "last_error": None,
    "snapshot": None,
}

mcp = FastMCP(
    APP_NAME,
    instructions=(
        
(
        "Herramientas para leer métricas reales de Garmin Connect. "
        "Responde siempre en español y prioriza términos canónicos alineados con Garmin Connect en español. "
        "Usa 'Predisposición para entrenar', 'VFC', 'Puntuación de sueño', 'Carga aguda' y 'Estrés'. "
        "NUNCA uses los acrónimos en inglés 'HRV', 'RHR' ni los términos 'Training Readiness', 'Training Effect' o 'Stamina': usa siempre 'VFC', 'FC en reposo', 'Predisposición para entrenar', 'Efecto de entrenamiento' y 'Energía disponible' respectivamente. "
        "Mantén 'Body Battery' como nombre propio de Garmin; si ayuda, puedes aclarar entre paréntesis 'energía corporal'. "
        "Traduce estados como FAIR->Aceptable, MODERATE->Moderada, BALANCED->Equilibrado, OPTIMAL->Óptimo y LOW->Bajo/Baja según contexto. "
        "Para tiempo de recuperación, usa siempre primero training_readiness_recovery_answer_for_llm o, si no existe, training_readiness_recovery_safe_text. "
        "No extrapoles manualmente. No conviertas descripciones cualitativas como 'Poca necesidad' en '0 minutos' salvo que exista un contador explícito de recuperación. "
        "Cuando el usuario pregunte por la hora de sincronización o de cuándo son los datos, prioriza ultima_sincronizacion_conector_local, snapshot_obtenido_local y datos_hasta_local.  Para Body Battery, usa el nombre 'Body Battery', no 'Batería corporal'. Para Predisposición para entrenar, usa estados en femenino: Muy baja, Baja, Moderada, Alta u Óptima según corresponda. Para sueño, prioriza sueno_texto_seguro, puntuacion_de_sueno y duracion_de_sueno_texto. No menciones REM, fases del sueño ni despertares salvo que existan campos canónicos explícitos para ello. "
        "Si el usuario pide máxima exactitud, usa get_raw_sources o get_cached_snapshot y responde basándote en raw_sources sin inventar campos. "
        "Si una métrica no existe, di que Garmin no la devolvió."
    )
),
)

# Aplica _translate_garmin a la salida de todos los tools automáticamente
if GARMIN_LANGUAGE.startswith("es"):
    _orig_mcp_tool = mcp.tool

    def _translating_tool(fn):
        @functools.wraps(fn)
        def _wrapped(*args, **kwargs):
            return _translate_garmin(fn(*args, **kwargs))
        return _orig_mcp_tool(_wrapped)

    mcp.tool = _translating_tool


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _now_local() -> datetime:
    return datetime.now(APP_TIMEZONE)


def _today_local() -> date:
    return _now_local().date()


def _isoish_to_local(value: Any) -> Any:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=APP_TIMEZONE)
        else:
            dt = dt.astimezone(APP_TIMEZONE)
        return dt.isoformat()
    except Exception:
        return value


def _format_duration_hm(seconds: Any) -> str | None:
    try:
        total = int(round(float(seconds)))
    except Exception:
        return None
    if total < 0:
        return None
    hours = total // 3600
    minutes = (total % 3600) // 60
    return f"{hours}h {minutes:02d}m"

def _short_local_dt_text(value: Any) -> str | None:
    dt = _parse_garmin_datetime(value) if value is not None else None
    if dt is None:
        return None
    return dt.strftime("%d/%m/%Y %H:%M")



def _normalize_readiness_status_es(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    mapping = {
        "very low": "Muy baja",
        "low": "Baja",
        "moderate": "Moderada",
        "high": "Alta",
        "optimal": "Óptima",
        "muy bajo": "Muy baja",
        "muy baja": "Muy baja",
        "bajo": "Baja",
        "baja": "Baja",
        "moderado": "Moderada",
        "moderada": "Moderada",
        "alto": "Alta",
        "alta": "Alta",
        "óptimo": "Óptima",
        "optimo": "Óptima",
        "óptima": "Óptima",
        "optima": "Óptima",
    }
    return mapping.get(raw.casefold(), raw)


def _build_sleep_safe_text(score: Any, duration_text: Any) -> str | None:
    if score is None and not duration_text:
        return None
    if score is not None and duration_text:
        return f"{score} puntos y {duration_text}"
    if score is not None:
        return f"{score} puntos"
    return str(duration_text)


def _json_loads_maybe_base64(raw: str) -> dict[str, Any]:
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


def _seed_token_file_if_needed() -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    if RESET_GARMIN_TOKENS and GARMIN_TOKENS_JSON:
        parsed = _json_loads_maybe_base64(GARMIN_TOKENS_JSON)
        TOKEN_FILE.write_text(json.dumps(parsed), encoding="utf-8")
        return

    if TOKEN_FILE.exists():
        return

    if not GARMIN_TOKENS_JSON:
        raise RuntimeError(
            "No existe token persistido y falta GARMIN_TOKENS_JSON en variables de entorno"
        )

    parsed = _json_loads_maybe_base64(GARMIN_TOKENS_JSON)
    TOKEN_FILE.write_text(json.dumps(parsed), encoding="utf-8")


# ---------------------------------------------------------------------------
# Re-login web wizard helpers (mobile-friendly /login flow)
# ---------------------------------------------------------------------------

import html as _html
import secrets as _secrets
import tempfile as _tempfile
import urllib.request as _urllib_request
import urllib.error as _urllib_error


def _is_first_run() -> bool:
    """True when there are no Garmin tokens persisted anywhere yet."""
    return not GARMIN_TOKENS_JSON and not TOKEN_FILE.exists()


def _admin_token_file() -> Path:
    return TOKEN_DIR / "admin_token"


def _current_admin_token() -> str:
    """Live admin password.

    Prefers the on-disk file (which the wizard can update WITHOUT a server
    restart, so password changes take effect instantly), and falls back to the
    ADMIN_TOKEN env var (used to survive container restarts on free tier).
    """
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


def _login_admin_ok(request: "Request") -> bool:
    """Open until a password is set; protected once a password exists.

    Model: while the user hasn't chosen a password, the setup and login pages are
    open to whoever has the URL — the initial, unprotected state, and the
    dashboard nudges the user to lock it down. Once a password is set, every
    sensitive page requires it (via ?token= or cookie).
    """
    admin = _current_admin_token()
    if not admin:
        return True
    cookie = request.cookies.get("admin_token", "")
    qp = request.query_params.get("token", "")
    if cookie and _secrets.compare_digest(cookie, admin):
        return True
    if qp and _secrets.compare_digest(qp, admin):
        return True
    return False


def _login_active_token(request: "Request") -> str:
    """Return the admin token if the request presented it (for cookie setting)."""
    admin = _current_admin_token()
    qp = request.query_params.get("token", "")
    if admin and qp and _secrets.compare_digest(qp, admin):
        return admin
    return ""


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


def _login_worker(session_id: str, email: str, password: str, user_id: str | None = None) -> None:
    """Background thread: drives the Garmin login flow, blocks on MFA when prompted."""
    session = _login_get_session(session_id)
    if not session:
        return

    mfa_event: threading.Event = session["mfa_event"]
    mfa_holder: dict[str, Any] = session["mfa_holder"]

    def prompt_mfa() -> str:
        session["status"] = "awaiting_mfa"
        ok = mfa_event.wait(timeout=LOGIN_MFA_TIMEOUT_SECONDS)
        if not ok:
            session["status"] = "error"
            session["error"] = "Tiempo agotado esperando el código MFA."
            raise RuntimeError("MFA timeout")
        return (mfa_holder.get("code") or "").strip()

    try:
        session["status"] = "starting"
        with _tempfile.TemporaryDirectory() as tmpdir:
            client = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
            client.login(tmpdir)
            token_path = Path(tmpdir) / "garmin_tokens.json"
            if not token_path.exists():
                raise RuntimeError("Login completado pero no se generó garmin_tokens.json")
            tokens_text = token_path.read_text(encoding="utf-8")
        # Validate JSON before persisting
        try:
            json.loads(tokens_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Tokens generados no son JSON válido: {exc}") from exc

        session["tokens"] = tokens_text
        persist_report = _persist_new_tokens(tokens_text, user_id=user_id)
        session["persist_report"] = persist_report
        session["status"] = "success"
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        if session.get("status") != "error":
            session["status"] = "error"
            session["error"] = str(exc) or exc.__class__.__name__


def _persist_new_tokens(tokens_text: str, user_id: str | None = None) -> dict[str, Any]:
    """Write tokens to disk. Returns a report dict for the UI.

    If user_id is given, tokens are written to that user's token dir (multi-user
    mode). Otherwise they go to the legacy TOKEN_FILE (single-user mode).
    """
    if user_id:
        return _persist_new_tokens_for_user(tokens_text, user_id)

    report: dict[str, Any] = {
        "disk": {"ok": False, "path": str(TOKEN_FILE), "error": None},
        "tokens_b64": base64.b64encode(tokens_text.encode("utf-8")).decode("ascii"),
    }

    # 1. Write to TOKEN_FILE so the running process picks up the new tokens immediately.
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Re-serialize compactly so what we write matches what the seed function expects.
        compact = json.dumps(json.loads(tokens_text))
        TOKEN_FILE.write_text(compact, encoding="utf-8")
        report["disk"]["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["disk"]["error"] = str(exc)

    return report


def _persist_new_tokens_for_user(tokens_text: str, user_id: str) -> dict[str, Any]:
    """Write Garmin tokens to a specific user's token directory."""
    report: dict[str, Any] = {
        "disk": {"ok": False, "path": str(_user_token_file(user_id)), "error": None},
        "tokens_b64": base64.b64encode(tokens_text.encode("utf-8")).decode("ascii"),
    }
    try:
        token_dir = _user_token_dir(user_id)
        token_dir.mkdir(parents=True, exist_ok=True)
        compact = json.dumps(json.loads(tokens_text))
        (token_dir / "garmin_tokens.json").write_text(compact, encoding="utf-8")
        report["disk"]["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["disk"]["error"] = str(exc)
    return report


def _login_render_page(
    title: str,
    step: int | None,
    body_html: str,
    *,
    auto_refresh_seconds: int | None = None,
    extra_head: str = "",
) -> str:
    """Wrap body in the shared mobile-first wizard chrome."""
    progress_html = ""
    if step is not None:
        dots = []
        for i in (1, 2, 3):
            cls = "dot dot-active" if i == step else ("dot dot-done" if i < step else "dot")
            dots.append(f'<span class="{cls}"></span>')
        progress_html = f'<div class="progress">{"".join(dots)}</div>'
    refresh_meta = (
        f'<meta http-equiv="refresh" content="{auto_refresh_seconds}">'
        if auto_refresh_seconds is not None
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><rect width=%22100%22 height=%22100%22 rx=%2220%22 fill=%22%230b0c0f%22/><rect x=%228%22 y=%2210%22 width=%2284%22 height=%2280%22 rx=%2210%22 fill=%22none%22 stroke=%22%231a1a22%22 stroke-width=%221.5%22/><rect x=%2216%22 y=%2222%22 width=%2256%22 height=%223%22 rx=%221.5%22 fill=%22%232a2a32%22/><rect x=%2216%22 y=%2230%22 width=%2240%22 height=%222.5%22 rx=%221.25%22 fill=%22%232a2a32%22/><rect x=%2216%22 y=%2240%22 width=%2264%22 height=%223%22 rx=%221.5%22 fill=%22%231f2128%22/><rect x=%2216%22 y=%2240%22 width=%2242%22 height=%223%22 rx=%221.5%22 fill=%22%23ff6b35%22/><rect x=%2216%22 y=%2248%22 width=%2264%22 height=%223%22 rx=%221.5%22 fill=%22%231f2128%22/><rect x=%2216%22 y=%2248%22 width=%2250%22 height=%223%22 rx=%221.5%22 fill=%22%2330d158%22/><rect x=%2216%22 y=%2256%22 width=%2264%22 height=%223%22 rx=%221.5%22 fill=%22%231f2128%22/><rect x=%2216%22 y=%2256%22 width=%2228%22 height=%223%22 rx=%221.5%22 fill=%22%230a84ff%22/><rect x=%2216%22 y=%2264%22 width=%2264%22 height=%223%22 rx=%221.5%22 fill=%22%231f2128%22/><rect x=%2216%22 y=%2264%22 width=%2236%22 height=%223%22 rx=%221.5%22 fill=%22%23c4a0ff%22/><rect x=%2216%22 y=%2272%22 width=%2264%22 height=%223%22 rx=%221.5%22 fill=%22%231f2128%22/><rect x=%2216%22 y=%2272%22 width=%2232%22 height=%223%22 rx=%221.5%22 fill=%22%23ff6b35%22/><path d=%22M76,40 Q62,33 60,22 Q58,11 65,8 Q72,4 76,11 Q80,4 87,8 Q94,11 92,22 Q90,33 76,40 Z%22 fill=%22none%22 stroke=%22%23ff3b30%22 stroke-width=%224%22 stroke-linejoin=%22round%22/><polyline points=%2262,22 67,22 69,18 71,29 72,22 74,26 76,22 78,22 80,18 81,29 83,22 85,26 87,22 90,22%22 fill=%22none%22 stroke=%22%23fff%22 stroke-width=%223.2%22 stroke-linecap=%22round%22 stroke-linejoin=%22round%22/></svg>">
{refresh_meta}
{extra_head}
<title>{_html.escape(title)}</title>
<style>
:root{{color-scheme:dark}}
*{{box-sizing:border-box}}
html,body{{margin:0;padding:0}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  background:#0d0d12;color:#f2f2f7;-webkit-font-smoothing:antialiased;
  line-height:1.5;min-height:100vh;font-size:16px;
}}
.wrap{{width:100%;max-width:640px;margin:0 auto;padding:28px 18px 72px}}
.card{{background:transparent;padding:0}}
h1{{margin:0 0 6px;font-size:26px;font-weight:700;letter-spacing:-.02em;color:#fff}}
h2{{margin:24px 0 10px;font-size:18px;font-weight:600;color:#fff}}
p{{margin:0 0 14px}}
.muted{{color:#9a9aa2;font-size:14px}}
.small{{font-size:12px}}
label{{display:block;font-size:14px;font-weight:600;margin:18px 0 6px;color:#e5e5ea}}
input{{
  width:100%;padding:15px 14px;font-size:17px;border:1px solid #3a3a44;border-radius:12px;
  background:#1c1c22;color:#fff;-webkit-appearance:none;appearance:none;
}}
input::placeholder{{color:#7a7a82}}
input:focus{{outline:none;border-color:#0a84ff;box-shadow:0 0 0 3px rgba(10,132,255,.25)}}
button{{
  width:100%;padding:15px;font-size:17px;font-weight:600;background:#0a84ff;color:#fff;border:0;border-radius:12px;
  margin-top:20px;cursor:pointer;-webkit-tap-highlight-color:transparent;
}}
button:active{{background:#0066cc}}
button.secondary{{background:#26262e;color:#0a84ff}}
button.secondary:active{{background:#33333d}}
.pw-wrap{{position:relative}}
.pw-wrap input{{padding-right:52px}}
.pw-toggle{{position:absolute;right:5px;top:5px;bottom:5px;width:42px;margin:0;padding:0;
  background:transparent;border:0;color:#9a9aa2;font-size:20px;line-height:1;cursor:pointer;
  display:flex;align-items:center;justify-content:center}}
.pw-toggle:active{{background:transparent}}
ol,ul{{padding-left:20px;line-height:1.7}}
li{{margin:5px 0}}
code{{background:rgba(255,255,255,.1);padding:2px 6px;border-radius:5px;font-size:.9em;word-break:break-word}}
.progress{{display:flex;justify-content:center;gap:8px;margin-bottom:24px}}
.dot{{width:8px;height:8px;border-radius:50%;background:#3a3a44;transition:all .2s}}
.dot-active{{background:#0a84ff;transform:scale(1.4)}}
.dot-done{{background:#30d158}}
.error{{background:rgba(255,69,58,.15);color:#ff6961;padding:12px 14px;border-radius:10px;margin:12px 0;font-size:14px;border:1px solid rgba(255,69,58,.3)}}
.success{{background:rgba(48,209,88,.15);color:#4cd964;padding:12px 14px;border-radius:10px;margin:12px 0;font-size:14px;border:1px solid rgba(48,209,88,.3)}}
.spinner{{width:36px;height:36px;border:3px solid #2a2a32;border-top-color:#0a84ff;border-radius:50%;
  animation:spin 1s linear infinite;margin:28px auto}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
pre{{background:#1c1c22;color:#f2f2f7;padding:14px;border-radius:10px;font-size:12px;
  overflow-x:auto;word-break:break-all;white-space:pre-wrap;border:1px solid #2a2a32}}
details{{margin:10px 0}}
summary{{cursor:pointer}}
a{{color:#0a84ff;text-decoration:none}}
a:active{{opacity:.7}}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    {progress_html}
    {body_html}
  </div>
</div>
</body>
</html>"""


def _password_input_html(
    input_id: str,
    name: str,
    *,
    placeholder: str = "",
    autocomplete: str = "current-password",
    minlength: int | None = None,
    autofocus: bool = False,
) -> str:
    """A password field hidden by default with an eye toggle to show/hide it."""
    attrs = (
        f'id="{input_id}" name="{name}" type="password" autocomplete="{autocomplete}" '
        'autocapitalize="off" autocorrect="off" spellcheck="false" required'
    )
    if minlength:
        attrs += f' minlength="{minlength}"'
    if autofocus:
        attrs += ' autofocus'
    if placeholder:
        attrs += f' placeholder="{_html.escape(placeholder)}"'
    toggle_js = (
        "var i=this.previousElementSibling;"
        "if(i.type==='password'){i.type='text';this.textContent='🙈';}"
        "else{i.type='password';this.textContent='👁';}"
    )
    return (
        '<div class="pw-wrap">'
        f'<input {attrs}>'
        f'<button type="button" class="pw-toggle" aria-label="Mostrar u ocultar contraseña" '
        f'onclick="{toggle_js}">👁</button>'
        '</div>'
    )


class _GarminActivityDownloadFormat:
    ORIGINAL = 1
    TCX = 2
    GPX = 3
    KML = 4
    CSV = 5


def _get_api(user_id: str | None = None) -> Garmin:
    if user_id is None:
        user = _get_auth_user()
        if user:
            user_id = user["id"]
    if user_id:
        token_dir = _user_token_dir(user_id)
        _seed_user_token_file(user_id, token_dir)
        api = Garmin()
        api.login(str(token_dir))
        return api
    _seed_token_file_if_needed()
    api = Garmin()
    api.login(str(TOKEN_DIR))
    return api


def _optional_call_first(api: Garmin, methods: tuple[str, ...], *args: Any) -> tuple[Any, str | None]:
    last_error = None
    attempted = False

    for name in methods:
        fn = getattr(api, name, None)
        if callable(fn):
            attempted = True
            try:
                return fn(*args), None
            except Exception as exc:
                last_error = f"{name}: {exc}"

    if not attempted:
        return None, None

    return None, last_error


def _optional_call_variants(
    api: Garmin,
    variants: list[tuple[tuple[str, ...], tuple[Any, ...]]],
) -> tuple[Any, str | None]:
    last_error = None
    attempted = False

    for methods, args in variants:
        for name in methods:
            fn = getattr(api, name, None)
            if callable(fn):
                attempted = True
                try:
                    return fn(*args), None
                except Exception as exc:
                    last_error = f"{name}{args}: {exc}"

    if not attempted:
        return None, None

    return None, last_error


def _parse_date(target_date: str | None) -> str:
    if not target_date:
        return _today_local().isoformat()
    return date.fromisoformat(target_date).isoformat()


def _resting_hr(heart_data: Any) -> Any:
    if not isinstance(heart_data, dict):
        return None

    if heart_data.get("restingHeartRate") is not None:
        return heart_data.get("restingHeartRate")

    try:
        return heart_data["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"][0]["value"]
    except Exception:
        return None


def _sleep_metrics(sleep_data: Any) -> dict[str, Any]:
    if not isinstance(sleep_data, dict):
        return {}

    daily = sleep_data.get("dailySleepDTO") or {}

    def sec_to_h(sec: Any) -> Any:
        try:
            return round(float(sec) / 3600, 2)
        except Exception:
            return None

    def sec_to_min(sec: Any) -> Any:
        try:
            return int(float(sec) / 60)
        except Exception:
            return None

    score = None
    try:
        score = daily["sleepScores"]["overall"]["value"]
    except Exception:
        score = None

    return {
        "sleep_hours": sec_to_h(daily.get("sleepTimeSeconds")),
        "sleep_score": score,
        "sleep_rem_min": sec_to_min(daily.get("remSleepSeconds")),
        "sleep_deep_min": sec_to_min(daily.get("deepSleepSeconds")),
        "sleep_light_min": sec_to_min(daily.get("lightSleepSeconds")),
        "sleep_awake_min": sec_to_min(daily.get("awakeSleepSeconds")),
    }


def _body_battery_metrics(bb_data: Any) -> dict[str, Any]:
    blocks = []
    if isinstance(bb_data, dict):
        blocks = [bb_data]
    elif isinstance(bb_data, list):
        blocks = [x for x in bb_data if isinstance(x, dict)]

    levels: list[float] = []
    charged = None
    drained = None
    last_timestamp_local = None
    feedback_level = None
    feedback_short = None
    feedback_long = None
    series = []

    for block in blocks:
        if charged is None and isinstance(block.get("charged"), (int, float)):
            charged = block.get("charged")

        if drained is None and isinstance(block.get("drained"), (int, float)):
            drained = block.get("drained")

        if last_timestamp_local is None and block.get("endTimestampLocal") is not None:
            last_timestamp_local = block.get("endTimestampLocal")

        feedback = block.get("bodyBatteryDynamicFeedbackEvent") or {}
        if feedback_level is None and feedback.get("bodyBatteryLevel") is not None:
            feedback_level = feedback.get("bodyBatteryLevel")
        if feedback_short is None and feedback.get("feedbackShortType") is not None:
            feedback_short = feedback.get("feedbackShortType")
        if feedback_long is None and feedback.get("feedbackLongType") is not None:
            feedback_long = feedback.get("feedbackLongType")

        values_array = block.get("bodyBatteryValuesArray")
        if isinstance(values_array, list):
            for item in values_array:
                if (
                    isinstance(item, list)
                    and len(item) >= 2
                    and isinstance(item[1], (int, float))
                    and 0 <= item[1] <= 100
                ):
                    levels.append(float(item[1]))
                    series.append({"timestamp_ms": item[0], "level": item[1]})
        elif isinstance(block.get("value"), (int, float)) and 0 <= block["value"] <= 100:
            levels.append(float(block["value"]))

    if not levels:
        return {
            "body_battery_current": None,
            "body_battery_max": None,
            "body_battery_min": None,
            "body_battery_charged": charged,
            "body_battery_drained": drained,
            "body_battery_last_timestamp_local": last_timestamp_local,
            "body_battery_feedback_level": feedback_level,
            "body_battery_feedback_short": feedback_short,
            "body_battery_feedback_long": feedback_long,
            "body_battery_series": series,
            "body_battery_raw": bb_data,
        }

    return {
        "body_battery_current": round(levels[-1]),
        "body_battery_max": round(max(levels)),
        "body_battery_min": round(min(levels)),
        "body_battery_charged": charged,
        "body_battery_drained": drained,
        "body_battery_last_timestamp_local": last_timestamp_local,
        "body_battery_feedback_level": feedback_level,
        "body_battery_feedback_short": feedback_short,
        "body_battery_feedback_long": feedback_long,
        "body_battery_series": series,
        "body_battery_raw": bb_data,
    }


def _stress_metrics(stress_data: Any) -> dict[str, Any]:
    if not isinstance(stress_data, dict):
        return {}

    def to_min(v: Any) -> Any:
        try:
            return int(float(v) / 60)
        except Exception:
            return None

    return {
        "stress_avg": stress_data.get("avgStressLevel"),
        "stress_max": stress_data.get("maxStressLevel"),
        "stress_duration_min": to_min(stress_data.get("stressDuration")),
        "rest_duration_min": to_min(stress_data.get("restStressDuration")),
    }


def _pick_first_present(container: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if not isinstance(container, dict):
            return None
        value = container.get(key)
        if value is not None:
            return value
    return None


def _hrv_metrics(hrv_data: Any) -> dict[str, Any]:
    if not isinstance(hrv_data, dict):
        return {}

    summary = hrv_data.get("hrvSummary") or {}
    if not isinstance(summary, dict):
        summary = {}

    return {
        "hrv_last_night": _pick_first_present(summary, ("lastNight", "lastNightAvg", "lastNightAverage")),
        "hrv_weekly_avg": _pick_first_present(summary, ("weeklyAvg", "sevenDayAvg", "baselineAvg")),
        "hrv_status": _pick_first_present(summary, ("hrvStatus", "status")),
        "hrv_baseline_low": _pick_first_present(summary, ("baselineBalancedLow", "balancedLow")),
        "hrv_baseline_high": _pick_first_present(summary, ("baselineBalancedHigh", "balancedHigh")),
    }


def _select_training_readiness_entry(training_readiness: Any) -> dict[str, Any] | None:
    if isinstance(training_readiness, dict):
        return training_readiness

    if isinstance(training_readiness, list):
        candidates = [e for e in training_readiness if isinstance(e, dict)]
        if not candidates:
            return None

        valid_sleep = [e for e in candidates if e.get("validSleep") is True]
        pool = valid_sleep or candidates

        def sort_key(entry: dict[str, Any]) -> tuple[int, str]:
            ts = entry.get("timestampLocal") or entry.get("timestamp") or ""
            return (1 if entry.get("inputContext") == "UPDATE_REALTIME_VARIABLES" else 0, str(ts))

        return sorted(pool, key=sort_key, reverse=True)[0]

    return None


TRAINING_READINESS_STATUS_ES = {
    "PRIMED": "Óptimo",
    "READY": "Listo",
    "GOOD": "Bueno",
    "MODERATE": "Moderada",
    "LOW": "Bajo",
    "POOR": "Muy bajo",
    "RECOVERY": "Recuperación",
    "REST": "Descanso",
    "WORKING_HARD": "Cargando fuerte",
    "BALANCE_YOUR_TRAINING_LOAD": "Equilibra tu carga de entrenamiento",
    "OVERREACHING": "Sobrecarga",
    "STRAINED": "Tensionado",
    "UNKNOWN": "Sin mensaje",
    "GOOD_RECOVERY": "Buena recuperación",
    "MOD_RT_LOW_SS_MOD_SLEEP_HISTORY_NEG": "Moderada — sueño reciente bajo",
    "HIGH_RT": "Alta disposición",
    "LOW_RT": "Baja disposición",
    "POOR_SLEEP": "Sueño insuficiente",
    "HIGH_STRESS_HISTORY": "Estrés acumulado alto",
}


def _translate_training_readiness_status(value: Any) -> Any:
    if value is None:
        return None
    key = str(value).strip().upper().replace(" ", "_")
    return TRAINING_READINESS_STATUS_ES.get(key, str(value).replace("_", " ").title())


def _training_readiness_metrics(training_readiness: Any) -> dict[str, Any]:
    entry = _select_training_readiness_entry(training_readiness)
    if not isinstance(entry, dict):
        return {}

    score = _pick_first_present(entry, (
        "score",
        "readinessScore",
        "trainingReadinessScore",
        "value",
    ))
    status = _pick_first_present(entry, (
        "level",
        "status",
        "readinessStatus",
        "shortFeedback",
        "feedbackShortType",
    ))
    message = _pick_first_present(entry, (
        "feedbackShort",
        "description",
        "message",
        "shortMessage",
        "fullMessage",
        "feedbackLong",
        "feedbackLongType",
    ))
    recovery_time = _pick_first_present(entry, (
        "recoveryTime",
        "recoveryHours",
    ))

    return {
        "training_readiness_score": score,
        "training_readiness_status": status,
        "training_readiness_status_es": _translate_training_readiness_status(status),
        "training_readiness_message": message,
        "training_readiness_message_es": _translate_training_readiness_status(message),
        "training_readiness_recovery_time": recovery_time,
        "training_readiness_input_context": entry.get("inputContext"),
        "training_readiness_selected_entry": entry,
    }


def _extract_vo2(max_metrics: Any, training_status: Any) -> Any:
    if isinstance(max_metrics, list) and max_metrics:
        try:
            value = max_metrics[0]["generic"]["vo2MaxPreciseValue"]
            if value is not None:
                return value
        except Exception:
            pass

    if isinstance(training_status, dict):
        try:
            generic = training_status["mostRecentVO2Max"]["generic"]
            value = generic.get("vo2MaxPreciseValue")
            if value is None:
                value = generic.get("vo2MaxValue")
            return value
        except Exception:
            return None

    return None


def _normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    activity_type = activity.get("activityType") or activity.get("activityTypeDTO") or {}
    summary = activity.get("summaryDTO") or {}
    type_key = activity_type.get("typeKey")

    duration_seconds = activity.get("duration")
    if duration_seconds is None:
        duration_seconds = summary.get("duration")

    distance_m = activity.get("distance")
    if distance_m is None:
        distance_m = summary.get("distance")

    return {
        "activity_id": activity.get("activityId"),
        "name": activity.get("activityName"),
        "type": type_key,
        "activity_family": _activity_family(type_key),
        "start_time_local": activity.get("startTimeLocal") or summary.get("startTimeLocal"),
        "duration_min": round((duration_seconds or 0) / 60, 1),
        "distance_km": round((distance_m or 0) / 1000, 2),
        "avg_hr": activity.get("averageHR") or summary.get("averageHR"),
        "max_hr": activity.get("maxHR") or summary.get("maxHR"),
        "calories": activity.get("calories") or summary.get("calories"),
        "training_load": activity.get("trainingLoad") or activity.get("activityTrainingLoad") or summary.get("activityTrainingLoad"),
        "elevation_gain_m": activity.get("elevationGain") or summary.get("elevationGain"),
        "training_effect": summary.get("trainingEffect"),
        "anaerobic_training_effect": summary.get("anaerobicTrainingEffect"),
        "average_power": activity.get("averagePower") or summary.get("averagePower"),
        "normalized_power": summary.get("normalizedPower"),
        "average_run_cadence": activity.get("averageRunCadence") or summary.get("averageRunCadence"),
        "steps": activity.get("steps") or summary.get("steps"),
    }


def _extract_primary_device_info(training_status: Any, devices_raw: Any) -> dict[str, Any]:
    device_id = None
    device_name = None
    image_url = None

    if isinstance(training_status, dict):
        try:
            latest = training_status["mostRecentTrainingStatus"]["latestTrainingStatusData"]
            if isinstance(latest, dict) and latest:
                key = next(iter(latest.keys()))
                device_id = int(key)
        except Exception:
            pass

        if device_id is None:
            try:
                balance = training_status["mostRecentTrainingLoadBalance"]["metricsTrainingLoadBalanceDTOMap"]
                if isinstance(balance, dict) and balance:
                    key = next(iter(balance.keys()))
                    device_id = int(key)
            except Exception:
                pass

        for path in [
            ("mostRecentTrainingStatus", "recordedDevices"),
            ("mostRecentTrainingLoadBalance", "recordedDevices"),
        ]:
            try:
                devices = training_status[path[0]][path[1]]
                if isinstance(devices, list):
                    for dev in devices:
                        if not isinstance(dev, dict):
                            continue
                        dev_id = dev.get("deviceId")
                        if device_id is None and dev_id is not None:
                            device_id = dev_id
                        if device_id is not None and dev_id == device_id:
                            device_name = dev.get("deviceName")
                            image_url = dev.get("imageURL")
                            break
                    if device_name:
                        break
            except Exception:
                pass

    if device_id is None and isinstance(devices_raw, list):
        for dev in devices_raw:
            if not isinstance(dev, dict):
                continue
            for key in ("deviceId", "id", "unitId"):
                if dev.get(key) is not None:
                    device_id = dev.get(key)
                    break
            if device_id is not None:
                device_name = dev.get("deviceName") or dev.get("displayName") or dev.get("modelName")
                image_url = dev.get("imageURL")
                break

    return {
        "primary_device_id": device_id,
        "primary_device_name": device_name,
        "primary_device_image_url": image_url,
    }


def _collect_extra_raw(
    api: Garmin,
    target_date: str,
    training_status: Any,
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    raw: dict[str, Any] = {}
    errors: dict[str, str] = {}

    extra_specs: dict[str, list[tuple[tuple[str, ...], tuple[Any, ...]]]] = {
        "spo2_raw": [
            (("get_spo2_data", "get_pulse_ox_data"), (target_date,)),
        ],
        "respiration_raw": [
            (("get_respiration_data",), (target_date,)),
        ],
        "floors_raw": [
            (("get_floors_data",), (target_date,)),
        ],
        "intensity_minutes_raw": [
            (("get_intensity_minutes_data", "get_intensity_minutes"), (target_date,)),
        ],
        "hydration_raw": [
            (("get_hydration_data",), (target_date,)),
            (("get_hydration_log",), tuple()),
        ],
        "body_composition_raw": [
            (("get_body_composition",), (target_date,)),
            (("get_weight_data",), (target_date,)),
        ],
        "user_profile_raw": [
            (("get_user_profile",), tuple()),
        ],
        "user_settings_raw": [
            (("get_user_settings",), tuple()),
        ],
        "devices_raw": [
            (("get_devices",), tuple()),
        ],
        "activities_for_date_raw": [
            (("get_activities_fordate", "get_activities_by_date"), (target_date,)),
        ],
        "solar_raw": [
            (("get_solar_data",), (target_date,)),
        ],
        "blood_pressure_raw": [
            (("get_blood_pressure_data",), (target_date,)),
        ],
        "resting_metabolic_rate_raw": [
            (("get_resting_metabolic_rate",), (target_date,)),
        ],
        "race_predictions_raw": [
            (("get_race_predictions",), tuple()),
        ],
        "fitness_age_raw": [
            (("get_fitnessage_data", "get_fitness_age"), (target_date,)),
        ],
        "personal_records_raw": [
            (("get_personal_records",), tuple()),
        ],
    }

    for key, variants in extra_specs.items():
        data, err = _optional_call_variants(api, variants)
        if data is not None:
            raw[key] = data
        elif err:
            errors[key] = err

    device_info = _extract_primary_device_info(training_status, raw.get("devices_raw"))
    primary_device_id = device_info.get("primary_device_id")

    if primary_device_id is not None:
        device_settings, device_settings_err = _optional_call_variants(
            api,
            [
                (("get_device_settings",), (primary_device_id,)),
            ],
        )
        if device_settings is not None:
            raw["device_settings_raw"] = device_settings
        elif device_settings_err:
            errors["device_settings_raw"] = device_settings_err

    return raw, errors, device_info


def _collect_day_snapshot(target_date: str, include_recent_activities: bool = False) -> dict[str, Any]:
    target_date = _parse_date(target_date)
    sleep_reference_day = target_date

    with FETCH_LOCK:
        api = _get_api()

        summary, summary_err = _optional_call_first(api, ("get_user_summary", "get_stats"), target_date)
        heart, heart_err = _optional_call_first(api, ("get_heart_rates", "get_rhr_day"), target_date)
        sleep, sleep_err = _optional_call_first(api, ("get_sleep_data",), sleep_reference_day)
        stress, stress_err = _optional_call_first(api, ("get_stress_data",), target_date)
        body_battery, bb_err = _optional_call_first(api, ("get_body_battery",), target_date)
        hrv, hrv_err = _optional_call_first(api, ("get_hrv_data",), target_date)
        max_metrics, vo2_err = _optional_call_first(api, ("get_max_metrics",), target_date)
        training_readiness, tr_err = _optional_call_first(api, ("get_training_readiness",), target_date)
        training_status, ts_err = _optional_call_first(api, ("get_training_status",), target_date)

        activities = []
        activities_raw = []
        activities_err = None
        if include_recent_activities:
            recent, activities_err = _optional_call_first(api, ("get_activities",), 0, ACTIVITY_LIMIT)
            if isinstance(recent, list):
                activities_raw = recent[:ACTIVITY_LIMIT]
                activities = [_normalize_activity(a) for a in recent[:ACTIVITY_LIMIT] if isinstance(a, dict)]

        extra_raw, extra_errors, device_info = _collect_extra_raw(api, target_date, training_status)

    metrics: dict[str, Any] = {
        "steps": (summary or {}).get("totalSteps"),
        "distance_km": round(((summary or {}).get("totalDistanceMeters") or 0) / 1000, 2),
        "active_kcal": (summary or {}).get("activeKilocalories"),
        "total_kcal": (summary or {}).get("totalKilocalories"),
        "resting_hr": _resting_hr(heart),
        "vo2max": _extract_vo2(max_metrics, training_status),
        "primary_device_id": device_info.get("primary_device_id"),
        "primary_device_name": device_info.get("primary_device_name"),
        "primary_device_image_url": device_info.get("primary_device_image_url"),
    }

    metrics.update(_sleep_metrics(sleep))
    metrics.update(_stress_metrics(stress))
    metrics.update(_body_battery_metrics(body_battery))
    metrics.update(_hrv_metrics(hrv))
    metrics.update(_training_readiness_metrics(training_readiness))

    if training_readiness is not None:
        metrics["training_readiness_raw"] = training_readiness
    if training_status is not None:
        metrics["training_status_raw"] = training_status

    raw_sources = {
        "summary_raw": summary,
        "heart_raw": heart,
        "sleep_raw": sleep,
        "stress_raw": stress,
        "body_battery_raw": body_battery,
        "hrv_raw": hrv,
        "max_metrics_raw": max_metrics,
        "training_readiness_raw": training_readiness,
        "training_status_raw": training_status,
        "recent_activities_raw": activities_raw,
        "primary_device_info_raw": device_info,
    }
    raw_sources.update(extra_raw)

    errors = {
        "summary": summary_err,
        "heart": heart_err,
        "sleep": sleep_err,
        "stress": stress_err,
        "body_battery": bb_err,
        "hrv": hrv_err,
        "vo2max": vo2_err,
        "training_readiness": tr_err,
        "training_status": ts_err,
        "activities": activities_err,
    }
    errors.update(extra_errors)
    errors = {k: v for k, v in errors.items() if v}

    # Si device_settings_raw ya vino bien, no mostramos error.
    if raw_sources.get("device_settings_raw") is not None:
        errors.pop("device_settings_raw", None)

    # Si el único problema era el método antiguo sin device_id, lo ocultamos
    # porque ahora la vía correcta es get_primary_device_info.
    if "device_settings_raw" in errors:
        msg = str(errors.get("device_settings_raw") or "")
        if "device_id" in msg or "missing 1 required positional argument" in msg:
            errors.pop("device_settings_raw", None)

    return {
        "date": target_date,
        "fetched_at": _now_iso(),
        "metrics": metrics,
        "recent_activities": activities,
        "raw_sources": raw_sources,
        "source_errors": errors,
    }


def _refresh_cache_sync() -> dict[str, Any]:
    try:
        snapshot = _collect_day_snapshot(_today_local().isoformat(), include_recent_activities=True)
        with CACHE_LOCK:
            CACHE["status"] = "ok"
            CACHE["last_refresh"] = _now_iso()
            CACHE["last_error"] = None
            CACHE["snapshot"] = snapshot
        return deepcopy(CACHE)
    except GarminConnectTooManyRequestsError as exc:
        with CACHE_LOCK:
            CACHE["status"] = "error"
            CACHE["last_error"] = f"429 Garmin rate limit: {exc}"
        return deepcopy(CACHE)
    except GarminConnectAuthenticationError as exc:
        with CACHE_LOCK:
            CACHE["status"] = "error"
            CACHE["last_error"] = f"Auth Garmin: {exc}"
        return deepcopy(CACHE)
    except GarminConnectConnectionError as exc:
        with CACHE_LOCK:
            CACHE["status"] = "error"
            CACHE["last_error"] = f"Conexión Garmin: {exc}"
        return deepcopy(CACHE)
    except Exception as exc:
        with CACHE_LOCK:
            CACHE["status"] = "error"
            CACHE["last_error"] = f"Error inesperado: {exc}"
        return deepcopy(CACHE)


def _background_refresh_loop() -> None:
    while True:
        _refresh_cache_sync()
        time.sleep(CACHE_MINUTES * 60)


def _dashboard_status() -> dict[str, Any]:
    """Return the state of each setup check displayed on the home dashboard."""
    now = time.time()

    # 1. Server is alive — always True if this function is being called.
    server_ok = True

    # 2. Garmin connected: tokens exist on disk or in env var, and last refresh worked.
    live_tokens = os.getenv("GARMIN_TOKENS_JSON", "").strip()
    has_tokens = TOKEN_FILE.exists() or bool(live_tokens)
    garmin_email = os.getenv("GARMIN_EMAIL", "").strip() or None
    with CACHE_LOCK:
        cache_status = CACHE.get("status")
        last_refresh = CACHE.get("last_refresh")
        last_error = CACHE.get("last_error")
    garmin_ok = bool(has_tokens)

    # 3. Persistence: tokens saved to disk (survives restarts if /data volume is mounted).
    persistence_ok = TOKEN_FILE.exists()

    # 4. Admin lock: a user-defined password protects /login after setup.
    admin_lock_ok = bool(_current_admin_token())

    # 5. MCP client connected: any /mcp hit recently.
    with _LAST_MCP_HIT_LOCK:
        last_mcp = _LAST_MCP_HIT
    claude_seen_minutes = None
    claude_ok = False
    mcp_client = _LAST_MCP_CLIENT
    if last_mcp:
        delta_min = (now - last_mcp) / 60
        claude_seen_minutes = int(delta_min)
        claude_ok = delta_min < 15

    return {
        "server_ok": server_ok,
        "garmin_ok": garmin_ok,
        "garmin_has_tokens": has_tokens,
        "garmin_email": garmin_email,
        "garmin_last_refresh": last_refresh,
        "garmin_last_error": last_error,
        "garmin_cache_status": cache_status,
        "persistence_ok": persistence_ok,
        "admin_lock_ok": admin_lock_ok,
        "claude_ok": claude_ok,
        "claude_seen_minutes": claude_seen_minutes,
        "mcp_client": mcp_client,
    }


def _public_base_url(request: "Request") -> str:
    """Build the externally-visible base URL, honoring reverse-proxy headers.

    Reverse proxies (Dokploy, Nginx, etc.) terminate TLS and forward requests
    internally as plain http, so request.url.scheme may be 'http'. We trust
    X-Forwarded-Proto / -Host to reconstruct the real https URL that Claude and
    the user actually use.
    """
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if not proto:
        proto = request.url.scheme
    if not host:
        host = request.url.netloc
    return f"{proto}://{host}"


def _human_time_ago(minutes: int | None) -> str:
    if minutes is None:
        return "nunca"
    if minutes < 1:
        return "hace segundos"
    if minutes < 60:
        return f"hace {minutes} min"
    hours = minutes // 60
    if hours < 24:
        return f"hace {hours} h"
    days = hours // 24
    return f"hace {days} d"


def _render_dashboard(request: "Request") -> str:
    """Build the home dashboard HTML based on current status."""
    s = _dashboard_status()
    public_url = _public_base_url(request)
    mcp_url = f"{public_url}/mcp"

    # ------ Status rows ------
    rows = _render_status_rows(s)

    # ------ Quick actions ------
    actions: list[str] = []
    if s["garmin_has_tokens"]:
        actions.append('<a href="/login"><button type="button">Re-loguear Garmin</button></a>')

    actions.append(
        f'<button type="button" onclick="navigator.clipboard.writeText({_html.escape(json.dumps(mcp_url))});this.innerText=\'Copiado ✓\'">Copiar URL del conector</button>'
    )

    if s["admin_lock_ok"]:
        actions.append('<a href="/lock"><button type="button" class="secondary">Bloquear sesión</button></a>')

    # ------ Inline guides (collapsible) ------
    guides: list[str] = []

    user_api_key = None
    user = _get_auth_user()
    if user:
        user_api_key = user.get("api_key")

    if user_api_key:
        connect_url = f"{public_url}/connect?api_key={user_api_key}"
        guides.append(
            '<details id="guide-connect" class="guide" open>'
            '<summary>🤖 Conectar tu IA (Claude Desktop, Móvil, ChatGPT…)</summary>'
            f'<p class="muted">Conecta el MCP desde cualquier dispositivo de forma fácil.</p>'
            '<div style="margin-top:10px">'
            f'<a href="{_html.escape(connect_url)}"><button type="button">Abrir wizard de conexión</button></a> '
            f'<button type="button" onclick="navigator.clipboard.writeText({_html.escape(json.dumps(connect_url))});this.innerText=\'Copiado ✓\'">Copiar link de conexión</button>'
            '</div>'
            f'<p style="margin-top:10px"><code style="font-size:12px">{_html.escape(user_api_key[:15])}•••••••</code> '
            f'<button type="button" onclick="navigator.clipboard.writeText({_html.escape(json.dumps(user_api_key))});this.innerText=\'Copiado ✓\'">Copiar API Key</button></p>'
            '<ol style="margin-top:6px">'
            '<li>Abre el <strong>wizard de conexión</strong> (botón de arriba)</li>'
            '<li>Elige tu cliente: Claude Desktop / Móvil / Code / Cursor</li>'
            '<li>Copia la configuración que se muestra</li>'
            '<li>Escanea el QR para conectar desde el móvil</li>'
            '</ol>'
            '</details>'
        )

    if not s["claude_ok"]:
        guides.append(
            '<details id="guide-claude" class="guide">'
            '<summary>Cómo conectar este MCP a tu IA (Claude, ChatGPT…)</summary>'
            '<p class="muted">Funciona con cualquier IA que acepte conectores MCP.</p>'
            '<ol>'
            '<li>Abre tu IA → <strong>Settings</strong> → sección de <strong>Connectors</strong> / MCP</li>'
            '<li>Elige <strong>Add custom connector</strong> (o "añadir servidor MCP")</li>'
            f'<li>Pega esta URL: <code>{_html.escape(mcp_url)}</code></li>'
            '<li>Guarda. En cuanto tu IA haga la primera petición, esta sección se pondrá verde ✅</li>'
            '</ol>'
            f'<button type="button" onclick="navigator.clipboard.writeText({_html.escape(json.dumps(mcp_url))});this.innerText=\'Copiado ✓\'">Copiar URL del conector</button>'
            '</details>'
        )
    elif user_api_key:
        guides.append(
            '<details id="guide-claude-ok" class="guide">'
            '<summary>✅ IA conectada recientemente</summary>'
            '<p class="muted">La última conexión fue recientemente. Puedes ampliar el wizard de conexión si necesitas conectar otro dispositivo.</p>'
            '</details>'
        )

    # ------ Compose ------
    body = (
        '<h1>Garmin Coach MCP</h1>'
        '<p class="muted">Panel de estado y configuración</p>'
        '<h2 style="margin-top:24px">Estado del setup</h2>'
        '<div id="status-rows" class="rows">' + "".join(rows) + '</div>'
        '<h2 style="margin-top:24px">Acciones</h2>'
        '<div class="actions">' + "".join(actions) + '</div>'
        + (('<h2 style="margin-top:24px">Configuración pendiente</h2>' + "".join(guides))
           if guides else '')
        + '<script>'
        'setInterval(async function(){'
        'try{var r=await fetch("/dashboard/rows");if(r.ok){'
        'document.getElementById("status-rows").innerHTML=await r.text();'
        '}}catch(e){}},1000)'
        '</script>'
    )

    extra_head = (
        '<style>'
        '.rows{display:flex;flex-direction:column;gap:10px}'
        '.row{display:flex;align-items:flex-start;gap:12px;padding:14px 16px;'
        'background:#16161c;border:1px solid #26262e;border-radius:12px}'
        '.row-ok{background:rgba(48,209,88,.10);border-color:rgba(48,209,88,.25)}'
        '.row-pending{background:rgba(255,159,10,.10);border-color:rgba(255,159,10,.28)}'
        '.row-icon{font-size:20px;line-height:1.3;flex:0 0 24px}'
        '.row-main{flex:1 1 auto;min-width:0}'
        '.row-title{font-weight:600;font-size:15px;color:#fff}'
        '.row-detail{font-size:13px;color:#9a9aa2;margin-top:3px;line-height:1.45}'
        '.row-action{flex:0 0 auto;align-self:center}'
        '.row-action button{margin-top:0;padding:9px 16px;font-size:14px;width:auto}'
        '.row-action a{display:inline-block}'
        '.actions{display:flex;flex-direction:column;gap:10px}'
        '.actions button{margin-top:0}'
        '.actions a{display:block}'
        'details.guide{background:#16161c;border:1px solid #26262e;border-radius:12px;'
        'padding:14px 16px;margin-top:10px}'
        'details.guide summary{cursor:pointer;font-weight:600;font-size:15px;padding:4px 0;color:#fff}'
        'details.guide ol{padding-left:20px;line-height:1.8;font-size:14px;margin:10px 0 0}'
        'details.guide li{margin:5px 0}'
        '@media (max-width:380px){'
        '.row{flex-wrap:wrap}'
        '.row-action{flex-basis:100%;margin-top:8px}'
        '.row-action button,.row-action a{width:100%}'
        '}'
        '</style>'
    )

    return _login_render_page("Garmin Coach MCP", None, body, extra_head=extra_head)


def _render_unlock_page(wrong: bool = False) -> str:
    err = '<div class="error">Contraseña incorrecta.</div>' if wrong else ''
    body = (
        '<h1>Panel protegido</h1>'
        '<p class="muted">Introduce tu contraseña para acceder a la configuración.</p>'
        f'{err}'
        '<form method="get" action="/admin">'
        '<label for="token">Contraseña</label>'
        + _password_input_html("token", "token", autocomplete="current-password", autofocus=True) +
        '<button type="submit">Entrar</button>'
        '</form>'
        '<details style="margin-top:28px">'
        '<summary>¿No recuerdas la contraseña?</summary>'
        '<p class="muted" style="margin-top:10px">Tu contraseña es el valor de la variable '
        '<code>ADMIN_TOKEN</code> definida al desplegar el servidor.</p>'
        '</details>'
    )
    return _login_render_page("Panel protegido", None, body)


def _request_is_https(request: "Request") -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme
    return proto == "https"


def _set_auth_cookie(response, token: str, request: "Request") -> None:
    """Set/refresh the long-lived sliding auth cookie (Instagram/X style)."""
    response.set_cookie(
        "admin_token", token, httponly=True, samesite="strict",
        max_age=AUTH_COOKIE_MAX_AGE_SECONDS, secure=_request_is_https(request),
    )


def _request_user(request: "Request") -> dict[str, Any] | None:
    """Return the user authenticated via the user_api_key cookie, if any."""
    api_key = request.cookies.get("user_api_key", "").strip()
    if not api_key:
        return None
    return _get_user_by_api_key(api_key)


def _set_user_cookie(response, api_key: str, request: "Request") -> None:
    response.set_cookie(
        "user_api_key", api_key, httponly=True, samesite="strict",
        max_age=AUTH_COOKIE_MAX_AGE_SECONDS, secure=_request_is_https(request),
    )


@mcp.custom_route("/dashboard/rows", methods=["GET"])
async def dashboard_rows(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    s = _dashboard_status()
    rows = _render_status_rows(s)
    return HTMLResponse("".join(rows))


def _render_status_rows(s: dict[str, Any]) -> list[str]:
    def row(ok: bool, title: str, detail: str = "", action_html: str = "") -> str:
        icon = "✅" if ok else "⚠️"
        cls = "row row-ok" if ok else "row row-pending"
        detail_html = f'<div class="row-detail">{detail}</div>' if detail else ""
        return (
            f'<div class="{cls}">'
            f'  <div class="row-icon">{icon}</div>'
            f'  <div class="row-main">'
            f'    <div class="row-title">{title}</div>'
            f'    {detail_html}'
            f'  </div>'
            f'  <div class="row-action">{action_html}</div>'
            f'</div>'
        )
    rows: list[str] = []
    rows.append(row(s["server_ok"], "Servidor desplegado", "Activo y respondiendo"))
    if s["garmin_ok"]:
        email_str = f" ({_html.escape(s['garmin_email'])})" if s["garmin_email"] else ""
        cache_fresh = s["garmin_cache_status"] == "ok"
        if cache_fresh:
            detail = "Tokens válidos, caché al día"
        elif s["garmin_last_error"]:
            detail = f"Tokens presentes, pero hay un error: {_html.escape(s['garmin_last_error'])}"
        else:
            detail = "Conectado, caché sin refrescar todavía"
        rows.append(row(True, f"Garmin conectado{email_str}", detail))
    else:
        rows.append(row(False, "Garmin sin conectar",
                        "Necesitas hacer login con tus credenciales Garmin",
                        '<a href="/login"><button type="button">Conectar</button></a>'))
    if s["persistence_ok"] and s["garmin_has_tokens"]:
        rows.append(row(True, "Guardado permanente en disco",
                        "Activado: tus tokens se guardan en el volumen de datos y aguantan reinicios."))
    elif s["persistence_ok"] and not s["garmin_has_tokens"]:
        rows.append(row(False, "Guardado permanente en disco",
                        "Volumen de datos listo, pero aún no hay tokens de Garmin que persistir. Conecta Garmin primero."))
    else:
        rows.append(row(False, "Guardado permanente en disco",
                        "No hay volumen de datos montado: si el servidor reinicia perderás la conexión con Garmin."))
    rows.append(row(s["admin_lock_ok"], "Protección con contraseña",
                    ("Activado: solo tú, con tu contraseña, puedes abrir el wizard de login."
                     if s["admin_lock_ok"]
                     else "Ahora mismo, cualquiera que sepa la dirección de tu servidor podría abrir esta página y cambiar la cuenta de Garmin conectada. Actívalo para protegerla con una contraseña que eliges tú."),
                    ('<a href="/setup/proteccion"><button type="button" class="secondary">Cambiar</button></a>'
                     if s["admin_lock_ok"]
                     else '<a href="/setup/proteccion"><button type="button" class="secondary">Activar</button></a>')))
    if s["claude_ok"]:
        ago = _human_time_ago(s["claude_seen_minutes"])
        client = s.get("mcp_client", "")
        detail = "Detectado por hits a /mcp"
        if client:
            short = client.split("/")[0] if "/" in client else client
            detail += f" · {short}"
        rows.append(row(True, f"IA conectada ({ago})", detail))
    elif s["claude_seen_minutes"] is not None:
        ago = _human_time_ago(s["claude_seen_minutes"])
        rows.append(row(True, f"IA durmiendo ({ago})",
                        "Sin actividad reciente. Cuando la IA vuelva a usarlo, se reactivará."))
    else:
        rows.append(row(True, "IA lista para conectar",
                        "El servidor MCP está esperando conexiones."))
    return rows


def _landing_html() -> str | None:
    """Look for the landing page close to the app; return its content or None."""
    for base in (Path(__file__).parent, Path(__file__).parent.parent):
        candidate = base / "index.html"
        if candidate.exists():
            try:
                return candidate.read_text(encoding="utf-8")
            except Exception:
                return None
    return None


@mcp.custom_route("/", methods=["GET"])
async def root(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    landing = _landing_html()
    if landing is not None:
        # Public marketing landing. Admin dashboard lives at /admin.
        return HTMLResponse(landing)

    # No landing file shipped → behave as the admin entry point.
    return await admin_dashboard(request)


@mcp.custom_route("/admin", methods=["GET"])
async def admin_dashboard(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse

    # If a valid token arrives in the URL, remember it as a cookie and clean the
    # URL so the password never lingers in the address bar.
    active = _login_active_token(request)
    if active:
        resp = RedirectResponse("/admin", status_code=303)
        _set_auth_cookie(resp, active, request)
        return resp

    # Protected and not authorized → ask for the password right here.
    admin = _current_admin_token()
    if admin and not _login_admin_ok(request):
        wrong = bool(request.query_params.get("token"))  # they tried a token and it didn't match
        return HTMLResponse(_render_unlock_page(wrong=wrong))

    resp = HTMLResponse(_render_dashboard(request))
    # Sliding session: refresh the cookie on every authorized visit, so it never
    # expires while the user keeps using it (like Instagram / X / TikTok).
    if admin:
        cookie = request.cookies.get("admin_token", "")
        if cookie and _secrets.compare_digest(cookie, admin):
            _set_auth_cookie(resp, admin, request)
    return resp


@mcp.custom_route("/lock", methods=["GET"])
async def lock_session(request: Request) -> Response:
    from starlette.responses import RedirectResponse
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie("admin_token", samesite="strict")
    return resp


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    with CACHE_LOCK:
        payload = {
            "status": "ok",
            "app": APP_NAME,
            "mcp_endpoint": "/mcp",
            "cache_status": CACHE["status"],
            "last_refresh": CACHE["last_refresh"],
            "last_refresh_local": _isoish_to_local(CACHE["last_refresh"]),
            "last_error": CACHE["last_error"],
            "token_file_exists": TOKEN_FILE.exists(),
            "volume_path": str(DATA_ROOT),
        }
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Connection wizard routes (multi-user)
# ---------------------------------------------------------------------------

def _get_public_url(request: Request) -> str:
    """Get the public base URL from the request."""
    host = request.headers.get("host", "localhost:8000")
    scheme = "https" if request.headers.get("x-forwarded-proto") == "https" or "https" in str(request.url) else "http"
    return f"{scheme}://{host}"


@mcp.custom_route("/connect", methods=["GET"])
async def connect_wizard(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    base_url = _get_public_url(request)
    api_key = request.query_params.get("api_key", "")

    desktop_json = '{"mcpServers": {"garmin-coach": {"command": "npx", "args": ["-y", "mcp-remote", "' + base_url + '/mcp", "--header", "X-User-API-Key:{API_KEY}"]}}}'
    cursor_json = '{"mcpServers": {"garmin-coach": {"url": "' + base_url + '/mcp", "headers": {"X-User-API-Key": "TU_API_KEY"}}}'

    html = (
        '<!DOCTYPE html>\n<html lang="es"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Conectar Garmin Coach MCP</title>\n'
        '<style>\n'
        '*{box-sizing:border-box;margin:0;padding:0}\n'
        'body{font-family:system-ui,-apple-system,sans-serif;background:#0a0a0a;color:#e0e0e0;padding:20px;max-width:800px;margin:0 auto}\n'
        'h1{color:#4ade80;margin-bottom:20px;font-size:1.8em}\n'
        'h2{color:#60a5fa;margin:24px 0 12px;font-size:1.3em}\n'
        '.card{background:#1a1a1a;border:1px solid #333;border-radius:12px;padding:20px;margin:16px 0}\n'
        'pre{background:#111;padding:16px;border-radius:8px;overflow-x:auto;font-size:13px;line-height:1.5;border:1px solid #333}\n'
        'code{color:#4ade80}\n'
        '.btn{background:#4ade80;color:#000;border:none;padding:10px 20px;border-radius:8px;cursor:pointer;font-weight:600;font-size:14px}\n'
        '.btn:hover{background:#22c55e}\n'
        '.btn-sm{padding:6px 12px;font-size:12px}\n'
        '.copied{background:#22c55e!important}\n'
        '.steps{counter-reset:step}\n'
        '.steps li{list-style:none;counter-increment:step;padding:8px 0 8px 40px;position:relative}\n'
        '.steps li::before{content:counter(step);position:absolute;left:0;width:28px;height:28px;background:#4ade80;color:#000;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px}\n'
        '.api-key-box{background:#111;padding:12px;border-radius:8px;border:1px solid #4ade80;display:flex;justify-content:space-between;align-items:center;word-break:break-all}\n'
        '.qr-img{background:white;padding:10px;border-radius:8px;display:inline-block}\n'
        '</style></head><body>\n'
        '<h1>Garmin Coach MCP - Conectar</h1>\n'

        '<h2>Tu API Key</h2>\n<div class="card">\n'
        '<div class="api-key-box"><code id="apikey">' + api_key + '</code>\n'
        '<button class="btn btn-sm" onclick="copyText(\'apikey\', this)">Copiar</button></div>\n'
        '<p style="margin-top:8px;color:#888;font-size:12px">Guarda esta API key. La necesitas para conectar desde cualquier cliente.</p>\n'
        '</div>\n'

        '<h2>1. Claude Desktop (Windows/Mac/Linux)</h2>\n<div class="card">\n'
        '<ol class="steps">\n'
        '<li>Abre Claude Desktop &gt; Settings &gt; Developer &gt; Edit Config</li>\n'
        '<li>Copia el siguiente JSON:</li>\n</ol>\n'
        '<pre id="desktop-config">' + _html.escape(desktop_json) + '</pre>\n'
        '<button class="btn" onclick="copyEl(\'desktop-config\', this)">Copiar configuracion</button>\n'
        '<p style="margin-top:8px;color:#888;font-size:12px">Reemplaza {API_KEY} con tu API key real.</p>\n'
        '</div>\n'

        '<h2>2. Claude.ai / Movil (Custom Connectors)</h2>\n<div class="card">\n'
        '<ol class="steps">\n'
        '<li>Abre <a href="https://claude.ai" style="color:#60a5fa">claude.ai</a> &gt; Settings &gt; Connectors</li>\n'
        '<li>Click "Add custom connector"</li>\n'
        '<li>URL del servidor:</li>\n</ol>\n'
        '<div class="api-key-box"><code>' + base_url + '/mcp</code>\n'
        '<button class="btn btn-sm" onclick="copyText(this.previousElementSibling, this)">Copiar URL</button></div>\n'
        '<p style="margin-top:12px">Headers: <code>X-User-API-Key</code> = tu API key</p>\n'
        '<p style="color:#888;font-size:12px;margin-top:4px">Una vez configurado, funciona en Desktop, web y movil.</p>\n'
        '</div>\n'

        '<h2>3. Claude Code CLI</h2>\n<div class="card">\n'
        '<pre>claude mcp add --transport http garmin-coach ' + base_url + '/mcp --header "X-User-API-Key: TU_API_KEY"</pre>\n'
        '<button class="btn" onclick="copyEl(this.previousElementSibling, this)">Copiar comando</button>\n'
        '</div>\n'

        '<h2>4. Cursor / VS Code / Windsurf</h2>\n<div class="card">\n'
        '<pre>' + _html.escape(cursor_json) + '</pre>\n'
        '<button class="btn" onclick="copyEl(this.previousElementSibling, this)">Copiar JSON</button>\n'
        '</div>\n'

        '<h2>5. QR Code (para movil)</h2>\n<div class="card">\n'
        '<p style="margin-bottom:12px">Escanea este codigo para abrir la pagina de conexion en tu movil:</p>\n'
        '<div class="qr-img"><img src="/api/qr?t=' + base_url + '/connect?api_key=' + api_key + '" width="200" height="200" alt="QR Code"></div>\n'
        '</div>\n'

        '<script>\n'
        'function copyText(id, btn){navigator.clipboard.writeText(typeof id==="string"?document.getElementById(id).textContent:id.textContent).then(()=>{btn.textContent="Copiado!";btn.classList.add("copied");setTimeout(()=>{btn.textContent="Copiar";btn.classList.remove("copied")},1500)})}\n'
        'function copyEl(el, btn){const t=typeof el==="string"?document.getElementById(el):el;navigator.clipboard.writeText(t.textContent).then(()=>{btn.textContent="Copiado!";btn.classList.add("copied");setTimeout(()=>{btn.textContent="Copiar";btn.classList.remove("copied")},1500)})}\n'
        '</script></body></html>'
    )
    return HTMLResponse(html)


@mcp.custom_route("/api/claude-config", methods=["GET"])
async def api_claude_config(request: Request) -> JSONResponse:
    api_key = request.query_params.get("api_key", "")
    base_url = _get_public_url(request)
    config = {
        "mcpServers": {
            "garmin-coach": {
                "command": "npx",
                "args": [
                    "-y", "mcp-remote",
                    f"{base_url}/mcp",
                    "--header", f"X-User-API-Key:{api_key}"
                ]
            }
        }
    }
    return JSONResponse(config)


@mcp.custom_route("/api/qr", methods=["GET"])
async def api_qr(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    url = request.query_params.get("t", request.query_params.get("url", ""))
    if not url:
        return HTMLResponse("Missing ?t=<url>", status_code=400)
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        svg_data = buf.getvalue().decode("utf-8")
        return HTMLResponse(svg_data, media_type="image/svg+xml")
    except Exception as e:
        return HTMLResponse(f"QR generation error: {e}", status_code=500)


@mcp.custom_route("/api/connection-test", methods=["GET"])
async def api_connection_test(request: Request) -> JSONResponse:
    api_key = request.query_params.get("api_key", "")
    user = _get_user_by_api_key(api_key) if api_key else None
    return JSONResponse({
        "status": "ok",
        "mcp_endpoint": "/mcp",
        "auth_configured": bool(api_key),
        "user_found": user is not None,
        "user_name": user.get("display_name") if user else None,
    })


# Admin user management routes

def _admin_page_html(title: str, body: str, extra_head: str = "") -> str:
    """Shared chrome for admin pages (dark / lime aesthetic)."""
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html.escape(title)}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--bg:#0a0d0b;--panel:#111710;--line:#1e2820;--fg:#e6ede8;--dim:#93a399;--faint:#5d6b63;--acc:#c8f169;--acc-dim:#8fa84e}}
body{{font-family:'Space Grotesk',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--fg);min-height:100vh;padding:32px 20px}}
.wrap{{max-width:880px;margin:0 auto}}
.back{{display:inline-flex;align-items:center;gap:.4rem;color:var(--dim);text-decoration:none;font-size:.85rem;margin-bottom:1.5rem;transition:color .15s}}
.back:hover{{color:var(--acc)}}
h1{{font-size:1.9rem;font-weight:700;letter-spacing:-.02em;margin-bottom:8px}}
h1 .acc{{color:var(--acc)}}
.sub{{color:var(--dim);font-size:.95rem;margin-bottom:2rem}}
.mono{{font-family:'IBM Plex Mono',monospace}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:24px;margin-bottom:20px}}
.card h2{{font-size:1.15rem;font-weight:600;margin-bottom:6px}}
.card .card-sub{{color:var(--dim);font-size:.88rem;margin-bottom:16px}}
label{{display:block;color:var(--dim);font-size:.85rem;font-weight:500;margin:16px 0 6px}}
input[type=text],input[type=email]{{width:100%;background:#0d120e;border:1px solid var(--line);color:var(--fg);padding:12px 14px;border-radius:10px;font-size:1rem;font-family:inherit;transition:border-color .15s}}
input:focus{{outline:none;border-color:var(--acc)}}
input::placeholder{{color:var(--faint)}}
.btn{{display:inline-flex;align-items:center;gap:.5rem;background:var(--acc);color:#0a0d0b;border:none;font-weight:600;font-family:inherit;font-size:1rem;padding:12px 22px;border-radius:10px;cursor:pointer;transition:filter .15s,transform .1s}}
.btn:hover{{filter:brightness(1.08)}}
.btn:active{{transform:translateY(1px)}}
.btn.secondary{{background:transparent;color:var(--fg);border:1px solid var(--line)}}
.btn.secondary:hover{{border-color:var(--acc);color:var(--acc);filter:none}}
.btn.block{{width:100%;justify-content:center}}
.badge{{display:inline-flex;align-items:center;gap:.4rem;background:rgba(200,241,105,.12);color:var(--acc);border:1px solid rgba(200,241,105,.25);padding:.25rem .7rem;border-radius:6px;font-size:.78rem;font-weight:500}}
.hint{{font-size:.8rem;color:var(--faint)}}
</style>
{extra_head}
</head>
<body>
<div class="wrap">
<a class="back" href="/admin">&larr; Volver al panel</a>
{body}
</div>
</body></html>"""


@mcp.custom_route("/admin/users", methods=["GET"])
async def admin_users_list(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    api_key = request.query_params.get("api_key", "")
    admin_key = ADMIN_API_KEY or api_key
    base_url = _get_public_url(request)

    db = _load_users_db()
    users = db.get("users", {})

    # Build table with real base_url
    rows = ""
    for uid, u in users.items():
        u_key = u.get("api_key", "")
        login_link = f"{base_url}/u/login?api_key={u_key}"
        key_display = u_key[:24] + "…"
        email = u.get("garmin_email", "")
        created = (u.get("created_at", "") or "")[:10]
        has_token = (u.get("garmin_tokens_json") or "").strip() != "" or (USERS_DB_DIR / uid / "garmin_tokens.json").exists()
        dot = "<span style=\"color:var(--acc)\">●</span>" if has_token else "<span style=\"color:var(--faint)\">○</span>"
        rows += f"""<tr>
<td><span class="mono" style="font-size:.8rem">{_html.escape(uid[:6])}</span></td>
<td>{_html.escape(u.get('display_name',''))}</td>
<td>{_html.escape(email) or '—'}</td>
<td><code style="font-size:.74rem;color:var(--acc-dim)">{_html.escape(key_display)}</code></td>
<td>{dot}</td>
<td style="font-size:.82rem;color:var(--dim)">{created}</td>
<td style="text-align:right"><a href="{_html.escape(login_link)}" class="btn secondary" style="font-size:.76rem;padding:6px 12px">Panel</a></td>
</tr>"""

    empty_state = (
        '<div class="card" style="text-align:center;padding:40px 24px">'
        '<div style="font-size:2.4rem;margin-bottom:10px">🏃</div>'
        '<h2 style="font-size:1.2rem">Todavía no hay usuarios</h2>'
        '<p class="card-sub" style="margin:8px auto 0;max-width:34em">'
        'Crea tu primer usuario con el asistente paso a paso de abajo. Recibirá su API key y su enlace de conexión.</p>'
        '</div>'
    ) if not rows else ""

    extra_head = (
        '<style>'
        'table{{width:100%;border-collapse:collapse;margin-top:8px}}'
        'th,td{{padding:12px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:.9rem}}'
        'th{{color:var(--dim);font-weight:500;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em}}'
        '.steps{{display:flex;flex-direction:column;gap:0;counter-reset:step;margin-top:8px}}'
        '</style>'
    )

    body = (
        '<h1>Usuarios <span class="acc">del club</span></h1>'
        '<p class="sub">Crea accesos para cada corredor. Cada uno conecta su propio Garmin de forma aislada.</p>'
        + ('<div class="card"><h2>Usuarios registrados</h2><div style="overflow-x:auto">'
           '<table><tr><th>ID</th><th>Nombre</th><th>Email Garmin</th><th>API Key</th><th>Estado</th><th>Creado</th><th></th></tr>'
           f'{rows}</table></div></div>' if rows else empty_state)
        + _admin_create_wizard_html()
    )
    return HTMLResponse(_admin_page_html("Admin · Usuarios", body, extra_head))


def _admin_create_wizard_html() -> str:
    """Multi-step, intuitive create-user wizard (JS-driven single page)."""
    return """
<div class="card" id="create-card">
  <h2>➕ Crear un nuevo corredor</h2>
  <p class="card-sub">Sigue los 3 pasos. Al terminar te daremos su clave y su enlace.</p>

  <div class="wiz-progress">
    <div class="wiz-step active" data-s="1"><span>1</span>Datos</div>
    <div class="wiz-step" data-s="2"><span>2</span>Revisar</div>
    <div class="wiz-step" data-s="3"><span>3</span>Listo</div>
  </div>

  <!-- STEP 1 -->
  <form id="step1">
    <label for="display_name">¿Quién es?</label>
    <input type="text" id="display_name" name="display_name" placeholder="ej. Carlos" autocomplete="off" required>
    <label for="garmin_email">Su email de Garmin <span class="hint">(opcional · lo rellena él later con su cuenta)</span></label>
    <input type="email" id="garmin_email" name="garmin_email" placeholder="ej. carlos@gmail.com" autocomplete="off">
    <button class="btn block" type="submit" style="margin-top:22px">Continuar <span class="mono">→</span></button>
  </form>

  <!-- STEP 2 -->
  <div id="step2" style="display:none">
    <div class="confirm-box">
      <p><span class="k">Nombre</span><span class="v" id="c-name">—</span></p>
      <p><span class="k">Email Garmin</span><span class="v" id="c-email">—</span></p>
    </div>
    <p class="hint" style="margin-top:14px">Pulsando "Crear" se generará una API key única para este usuario.</p>
    <div style="display:flex;gap:12px;margin-top:20px;flex-wrap:wrap">
      <button class="btn" type="button" id="wiz-back1">&larr; Atrás</button>
      <button class="btn" type="button" id="wiz-submit">Crear usuario</button>
    </div>
  </div>

  <!-- STEP 3 result injected here -->
  <div id="step3" style="display:none"></div>
</div>

<style>
.wiz-progress{display:flex;gap:0;margin:18px 0 8px;list-style:none}
.wiz-step{flex:1;text-align:center;font-size:.78rem;color:var(--faint);padding-bottom:10px;position:relative;border-bottom:2px solid var(--line)}
.wiz-step.active{color:var(--acc);border-bottom-color:var(--acc);font-weight:600}
.wiz-step span{display:inline-flex;width:22px;height:22px;border-radius:50%;background:var(--line);color:var(--dim);align-items:center;justify-content:center;font-size:.75rem;margin-right:6px}
.wiz-step.active span{background:var(--acc);color:#0a0d0b}
.confirm-box{background:#0d120e;border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.confirm-box p{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--line)}
.confirm-box p:last-child{border-bottom:none}
.confirm-box .k{color:var(--dim);font-size:.85rem}
.confirm-box .v{font-family:'IBM Plex Mono',monospace;color:var(--fg);word-break:break-all}
</style>

<script>
(function(){
  var data={};
  var lastKey='';
  var s1=document.getElementById('step1'),
      s2=document.getElementById('step2'),
      s3=document.getElementById('step3'),
      prog=document.querySelectorAll('.wiz-step');
  window.wizCopyKey=function(){
    if(lastKey){navigator.clipboard.writeText(lastKey).then(function(){alert('Clave copiada ✓')}).catch(function(){prompt('Copia la clave manualmente:',lastKey)})}
  };
  function setStep(n){
    prog.forEach(function(p){p.classList.toggle('active',parseInt(p.getAttribute('data-s'),10)===n)});
    s1.style.display=n===1?'':'none';
    s2.style.display=n===2?'':'none';
    s3.style.display=n===3?'':'none';
  }
  s1.addEventListener('submit',function(e){
    e.preventDefault();
    data.name=document.getElementById('display_name').value.trim();
    data.email=document.getElementById('garmin_email').value.trim();
    if(!data.name)return;
    document.getElementById('c-name').textContent=data.name;
    document.getElementById('c-email').textContent=data.email||'—';
    setStep(2);
  });
  document.getElementById('wiz-back1').addEventListener('click',function(){setStep(1)});
  document.getElementById('wiz-submit').addEventListener('click',function(){
    var fd=new FormData();
    fd.append('display_name',data.name);
    fd.append('garmin_email',data.email||'');
    fd.append('json','1');
    fetch('/admin/users/create',{method:'POST',body:fd})
      .then(function(r){return r.json()})
      .then(function(res){
        if(res.ok){
          lastKey=res.api_key||'';
          document.getElementById('step3').innerHTML=res.html;
          setStep(3);
        } else {
          alert(res.error||'Error al crear el usuario');
        }
      })
      .catch(function(){alert('Error de red al crear el usuario')});
  });
})();
</script>
"""


@mcp.custom_route("/admin/users/create", methods=["POST"])
async def admin_users_create(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    key = request.query_params.get("api_key", "")
    admin_key = ADMIN_API_KEY or key
    if ADMIN_API_KEY and key != ADMIN_API_KEY:
        return RedirectResponse("/admin/users", status_code=303)
    form = await request.form()
    display_name = (form.get("display_name") or "").strip()
    garmin_email = (form.get("garmin_email") or "").strip()
    want_json = (form.get("json") or "").strip() == "1"
    if not display_name:
        if want_json:
            return JSONResponse({"ok": False, "error": "El nombre es obligatorio."})
        return RedirectResponse("/admin/users", status_code=303)
    user = _create_user(display_name=display_name, garmin_email=garmin_email)

    base_url = _get_public_url(request)
    api_key = user.get("api_key", "")
    panel_link = f"{base_url}/u/login?api_key={api_key}"
    connect_link = f"{base_url}/connect?api_key={api_key}"

    if want_json:
        # Step-3 success panel rendered server-side for the wizard.
        success = (
            '<div style="text-align:center">'
            '<div style="font-size:2.6rem;margin-bottom:8px">🎉</div>'
            '<h2 style="font-size:1.3rem;color:var(--acc)">¡Listo! Corredor creado</h2>'
            '<p class="card-sub" style="margin-top:6px">Envía esto a <strong>' + _html.escape(display_name) + '</strong>:</p>'
            '</div>'
            '<div class="code-block" style="background:#0d120e;border:1px solid rgba(200,241,105,.25);border-radius:10px;padding:16px;margin-top:16px;text-align:left">'
            '<div style="font-size:.72rem;color:var(--faint);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Su API key</div>'
            '<div class="mono" style="word-break:break-all;font-size:.85rem;color:var(--acc)">' + _html.escape(api_key) + '</div>'
            '<button class="btn secondary" style="font-size:.78rem;padding:8px 14px;margin-top:14px" onclick="wizCopyKey()">Copiar clave</button>'
            '</div>'
            '<div style="text-align:left;margin-top:20px;display:flex;flex-direction:column;gap:12px">'
            '<p class="hint" style="margin:0"><strong style="color:var(--fg)">Paso 1 ·</strong> Comparte tu API key.</p>'
            '<p class="hint" style="margin:0"><strong style="color:var(--fg)">Paso 2 ·</strong> El corredor entra a conectar su Garmin:</p>'
            '<a href="' + _html.escape(panel_link) + '" class="btn secondary" style="font-size:.85rem;padding:10px 16px;justify-content:flex-start">Panel del corredor &rarr;</a>'
            '<p class="hint" style="margin:0"><strong style="color:var(--fg)">Paso 3 ·</strong> Y configura la IA con su clave:</p>'
            '<a href="' + _html.escape(connect_link) + '" class="btn secondary" style="font-size:.85rem;padding:10px 16px;justify-content:flex-start">Asistente para conectar la IA &rarr;</a>'
            '</div>'
        )
        return JSONResponse({"ok": True, "html": success, "api_key": api_key})

    # Non-JS fallback: full success page.
    body = (
        '<h1>Corredor <span class="acc">creado</span></h1>'
        '<p class="sub">Estos son los accesos de <strong>' + _html.escape(display_name) + '</strong>. Guárdalos y compártelos.</p>'
        '<div class="card"><h2>API key</h2>'
        '<p class="card-sub">La clave personal del corredor:</p>'
        '<div class="mono" style="background:#0d120e;border:1px solid var(--line);border-radius:10px;padding:14px;word-break:break-all;color:var(--acc)">' + _html.escape(api_key) + '</div>'
        '</div>'
        '<div class="card"><h2>Enlace de su panel</h2>'
        '<p class="card-sub">Para conectar su Garmin y ver su estado:</p>'
        '<a class="btn block" href="' + _html.escape(panel_link) + '" style="text-decoration:none">Abrir panel del corredor</a>'
        '</div>'
        '<div class="card"><h2>Conectar la IA</h2>'
        '<p class="card-sub">Para configurar Claude / Cursor / móvil:</p>'
        '<a class="btn block secondary" href="' + _html.escape(connect_link) + '" style="text-decoration:none">Asistente de conexión</a>'
        '</div>'
        '<a href="/admin/users" class="btn secondary" style="margin-top:8px;text-decoration:none">&larr; Volver a usuarios</a>'
    )
    return HTMLResponse(_admin_page_html("Usuario creado", body))


# ---------------------------------------------------------------------------
# Per-user web panel (API-key based) + Garmin connection wizard
# ---------------------------------------------------------------------------

def _require_user(request: "Request"):
    """Return the authenticated user from the user_api_key cookie, or None."""
    return _request_user(request)


def _user_unauthorized_html() -> str:
    body = (
        '<h1>Necesitas identificarte</h1>'
        '<p class="muted">Abre el enlace de conexión que te han enviado '
        '(contiene tu <code>api_key</code>) para acceder a tu panel.</p>'
    )
    return _login_render_page("Login", None, body)


@mcp.custom_route("/u/login", methods=["GET"])
async def user_login_start(request: Request) -> Response:
    """Authenticate a user by API key (from URL) and set their session cookie."""
    from starlette.responses import RedirectResponse
    api_key = request.query_params.get("api_key", "").strip()
    if not api_key:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)
    user = _get_user_by_api_key(api_key)
    if not user:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)
    resp = RedirectResponse("/panel", status_code=303)
    _set_user_cookie(resp, api_key, request)
    return resp


@mcp.custom_route("/panel", methods=["GET"])
async def user_panel(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    user = _require_user(request)
    if not user:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)

    user_id = user["id"]
    api_key = user.get("api_key", "")
    display_name = user.get("display_name", "") or user_id
    garmin_email = user.get("garmin_email", "")

    token_file = _user_token_file(user_id)
    connected = token_file.exists()
    public_url = _public_base_url(request)
    connect_url = f"{public_url}/connect?api_key={api_key}"

    garmin_status = (
        ('<div class="success">✅ Garmin conectado</div>'
         f'<p class="muted">Cuenta: {_html.escape(garmin_email or "—")}</p>')
        if connected
        else ('<div class="pending">⚠️ Garmin NO conectado</div>'
              '<p class="muted">Conecta tu cuenta de Garmin para que la IA pueda leer tus datos.</p>')
    )

    connect_btn = (
        '<a href="/garmin-connect"><button type="button">Conectar Garmin</button></a>'
        if not connected
        else '<a href="/garmin-connect"><button type="button" class="secondary">Re-conectar Garmin</button></a>'
    )

    body = (
        '<h1>Hola, ' + _html.escape(display_name) + '</h1>'
        '<p class="muted">Tu panel personal de Garmin Coach MCP</p>'
        '<h2 style="margin-top:24px">Estado de Garmin</h2>'
        + garmin_status
        + '<div style="margin-top:12px">' + connect_btn + '</div>'
        '<h2 style="margin-top:32px">Tu API Key</h2>'
        '<div class="api-key-box" style="background:#1c1c22;padding:12px;border-radius:10px;border:1px solid #3a3a44;word-break:break-all;display:flex;justify-content:space-between;align-items:center">'
        f'<code id="apikey">{_html.escape(api_key)}</code>'
        '<button type="button" onclick="navigator.clipboard.writeText(document.getElementById(\'apikey\').innerText);this.innerText=\'Copiada ✓\'">Copiar</button>'
        '</div>'
        '<h2 style="margin-top:32px">Conectar la IA</h2>'
        '<p class="muted">Abre el asistente de conexión para configurar Claude Desktop, el móvil o Cursor.</p>'
        '<div style="margin-top:12px">'
        f'<a href="{_html.escape(connect_url)}"><button type="button">Abrir asistente de conexión</button></a> '
        f'<button type="button" class="secondary" onclick="navigator.clipboard.writeText({_html.escape(json.dumps(connect_url))});this.innerText=\'Copiado ✓\'">Copiar link</button>'
        '</div>'
        '<p style="margin-top:24px"><a href="/logout"><button type="button" class="secondary">Cerrar sesión</button></a></p>'
    )

    extra_head = (
        '<style>'
        '.api-key-box button{margin-top:0;padding:8px 14px;font-size:13px}'
        '.pending{background:rgba(255,159,10,.12);border-radius:10px;padding:12px 14px;border:1px solid rgba(255,159,10,.3)}'
        '</style>'
    )
    return HTMLResponse(_login_render_page("Mi panel", None, body, extra_head=extra_head))


@mcp.custom_route("/logout", methods=["GET"])
async def user_logout(request: Request) -> Response:
    from starlette.responses import RedirectResponse
    resp = RedirectResponse("/u/login", status_code=303)
    resp.delete_cookie("user_api_key", samesite="strict")
    return resp


@mcp.custom_route("/garmin-connect", methods=["GET"])
async def garmin_connect_form(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    user = _require_user(request)
    if not user:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)

    body = (
        '<h1>Conectar con Garmin</h1>'
        '<p class="muted">Introduce las credenciales de tu cuenta Garmin Connect. '
        'Luego te pediremos el código MFA que enviamos a tu Gmail.</p>'
        '<form method="POST" action="/garmin-connect/submit">'
        '<label for="email">Email Garmin</label>'
        '<input id="email" name="email" type="email" autocomplete="username" required autofocus>'
        '<label for="password">Contraseña</label>'
        + _password_input_html("password", "password", autocomplete="current-password") +
        '<button type="submit">Siguiente</button>'
        '</form>'
        '<p style="margin-top:8px"><a href="/panel" style="color:#60a5fa">← Volver a mi panel</a></p>'
    )
    return HTMLResponse(_login_render_page("Conectar Garmin · paso 1", 1, body))


@mcp.custom_route("/garmin-connect/submit", methods=["POST"])
async def garmin_connect_submit(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    user = _require_user(request)
    if not user:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)

    form = await request.form()
    email = (form.get("email") or "").strip()
    password = (form.get("password") or "").strip()
    if not email or not password:
        body = (
            '<h1>Faltan datos</h1>'
            '<div class="error">Email o contraseña vacíos.</div>'
            '<a href="/garmin-connect">← Volver</a>'
        )
        return HTMLResponse(_login_render_page("Error", 1, body), status_code=400)

    session_id = _secrets.token_urlsafe(18)
    mfa_event = threading.Event()
    mfa_holder: dict[str, Any] = {"code": None}
    session = {
        "status": "starting",
        "error": None,
        "tokens": None,
        "persist_report": None,
        "mfa_event": mfa_event,
        "mfa_holder": mfa_holder,
        "user_id": user["id"],
        "created_at": time.time(),
    }
    _login_set_session(session_id, session)

    thread = threading.Thread(
        target=_login_worker,
        args=(session_id, email, password, user["id"]),
        daemon=True,
        name=f"garmin-connect-{session_id[:6]}",
    )
    session["thread"] = thread
    thread.start()

    return RedirectResponse(f"/garmin-connect/wait?session={session_id}", status_code=303)


@mcp.custom_route("/garmin-connect/wait", methods=["GET"])
async def garmin_connect_wait(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    user = _require_user(request)
    if not user:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)

    session_id = request.query_params.get("session", "").strip()
    session = _login_get_session(session_id)
    if not session or session.get("user_id") != user["id"]:
        body = (
            '<h1>Sesión caducada</h1>'
            '<p class="muted">Esta sesión de conexión expiró.</p>'
            '<a href="/garmin-connect">← Empezar de nuevo</a>'
        )
        return HTMLResponse(_login_render_page("Sesión caducada", None, body), status_code=410)

    status = session.get("status", "starting")
    if status == "awaiting_mfa":
        return RedirectResponse(f"/garmin-connect/mfa?session={session_id}", status_code=303)
    if status in ("success", "error"):
        return RedirectResponse(f"/garmin-connect/result?session={session_id}", status_code=303)

    body = (
        '<h2>Conectando con Garmin…</h2>'
        '<p class="muted">Estamos solicitando el código MFA. Revisa tu Gmail en unos segundos.</p>'
        '<div class="spinner" aria-hidden="true"></div>'
        '<p class="muted" style="text-align:center;font-size:13px">Esta página se actualizará sola.</p>'
    )
    return HTMLResponse(_login_render_page("Procesando", 1, body, auto_refresh_seconds=2))


@mcp.custom_route("/garmin-connect/mfa", methods=["GET"])
async def garmin_connect_mfa_form(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    user = _require_user(request)
    if not user:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)

    session_id = request.query_params.get("session", "").strip()
    session = _login_get_session(session_id)
    if not session or session.get("user_id") != user["id"]:
        body = (
            '<h1>Sesión caducada</h1>'
            '<p class="muted">Esta sesión expiró.</p>'
            '<a href="/garmin-connect">← Empezar de nuevo</a>'
        )
        return HTMLResponse(_login_render_page("Sesión caducada", None, body), status_code=410)

    status = session.get("status", "starting")
    if status in ("success", "error"):
        return RedirectResponse(f"/garmin-connect/result?session={session_id}", status_code=303)
    if status != "awaiting_mfa":
        return RedirectResponse(f"/garmin-connect/wait?session={session_id}", status_code=303)

    sid_safe = _html.escape(session_id)
    body = (
        '<h2>Código de verificación</h2>'
        '<p class="muted">Hemos pedido a Garmin que te envíe un código de 6 dígitos al Gmail. Revisa la bandeja de entrada y pégalo aquí.</p>'
        '<form method="POST" action="/garmin-connect/mfa">'
        f'<input type="hidden" name="session" value="{sid_safe}">'
        '<label for="code">Código MFA</label>'
        '<input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]*" '
        'autocomplete="one-time-code" maxlength="10" required autofocus>'
        '<button type="submit">Siguiente</button>'
        '</form>'
    )
    return HTMLResponse(_login_render_page("Conectar Garmin · paso 2", 2, body))


@mcp.custom_route("/garmin-connect/mfa", methods=["POST"])
async def garmin_connect_mfa_submit(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    user = _require_user(request)
    if not user:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)

    form = await request.form()
    session_id = (form.get("session") or "").strip()
    code = (form.get("code") or "").strip()
    session = _login_get_session(session_id)
    if not session or session.get("user_id") != user["id"]:
        body = (
            '<h1>Sesión caducada</h1>'
            '<p class="muted">Esta sesión expiró.</p>'
            '<a href="/garmin-connect">← Empezar de nuevo</a>'
        )
        return HTMLResponse(_login_render_page("Sesión caducada", None, body), status_code=410)
    if not code:
        return RedirectResponse(f"/garmin-connect/mfa?session={session_id}", status_code=303)

    session["mfa_holder"]["code"] = code
    session["mfa_event"].set()
    return RedirectResponse(f"/garmin-connect/result?session={session_id}", status_code=303)


@mcp.custom_route("/garmin-connect/result", methods=["GET"])
async def garmin_connect_result(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    user = _require_user(request)
    if not user:
        return HTMLResponse(_user_unauthorized_html(), status_code=401)

    session_id = request.query_params.get("session", "").strip()
    session = _login_get_session(session_id)
    if not session or session.get("user_id") != user["id"]:
        body = (
            '<h1>✅ Garmin conectado</h1>'
            '<p>Tu cuenta Garmin ya quedó guardada.</p>'
            '<a href="/panel"><button type="button">Ir a mi panel</button></a>'
        )
        return HTMLResponse(_login_render_page("Completado", None, body))

    status = session.get("status", "starting")
    if status not in ("success", "error"):
        body = (
            '<h2>Procesando…</h2>'
            '<p class="muted">Validando el código MFA con Garmin. Aguanta un momento.</p>'
            '<div class="spinner" aria-hidden="true"></div>'
        )
        return HTMLResponse(_login_render_page("Procesando", 3, body, auto_refresh_seconds=2))

    if status == "error":
        err = _html.escape(session.get("error") or "Error desconocido")
        body = (
            '<h1>Algo falló</h1>'
            f'<div class="error">{err}</div>'
            '<p class="muted">Causas típicas: contraseña incorrecta, código MFA caducado, o Garmin pidiendo CAPTCHA. Prueba de nuevo.</p>'
            '<a href="/garmin-connect"><button type="button">Empezar de nuevo</button></a>'
            '<p style="margin-top:8px"><a href="/panel" style="color:#60a5fa">← Ir a mi panel</a></p>'
        )
        _login_drop_session(session_id)
        return HTMLResponse(_login_render_page("Error", 3, body), status_code=400)

    _login_drop_session(session_id)
    body = (
        '<h1>✅ ¡Garmin conectado!</h1>'
        '<div class="success">Tu cuenta de Garmin quedó guardada en tu perfil. '
        'La IA ya puede leer tus datos de entrenamiento.</div>'
        '<div style="margin-top:16px">'
        '<a href="/panel"><button type="button">Ir a mi panel</button></a> '
        '<a href="/connect?api_key=' + _html.escape(user.get("api_key", "")) + '"><button type="button" class="secondary">Configurar la IA</button></a>'
        '</div>'
    )
    return HTMLResponse(_login_render_page("Listo", 3, body))


# Continue with existing routes

@mcp.custom_route("/download/{activity_id}", methods=["GET"])
async def download_activity_fit(request: Request) -> Response:
    import urllib.request as _urllib
    activity_id = request.path_params.get("activity_id")

    # Intenta primero con tokens locales
    try:
        with FETCH_LOCK:
            api = _get_api()
            data = None
            fmt_name = "zip"
            for fmt in [api.ActivityDownloadFormat.ORIGINAL, api.ActivityDownloadFormat.TCX, api.ActivityDownloadFormat.GPX]:
                try:
                    data = api.download_activity(activity_id, fmt)
                    if data:
                        fmt_name = "zip" if fmt == api.ActivityDownloadFormat.ORIGINAL else fmt.name.lower()
                        break
                except Exception:
                    continue
        if data:
            return Response(
                content=data,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="activity_{activity_id}.{fmt_name}"'},
            )
    except Exception:
        pass

    panel_url_dl = SELF_PUBLIC_URL or "el panel del servidor"
    return JSONResponse({"error": "Sesión de Garmin caducada", "fix": f"Abre {panel_url_dl} en tu navegador y entra en 'Re-loguear Garmin' (tarda 1 minuto)."}, status_code=503)


# Optional fallback to another running instance when local tokens fail. Empty by
# default — deployments must NOT proxy to anyone else's server.
SELF_PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

_WEB_CONFIG_FILE = DATA_ROOT / "web_config.json"
_WEB_CONFIG_ALLOWED_KEYS = {"driveUrl"}
_CONFIG_SHARES_DIR = DATA_ROOT / "config_shares"


@mcp.custom_route("/config", methods=["GET"])
async def get_web_config(_: Request) -> JSONResponse:
    try:
        if _WEB_CONFIG_FILE.exists():
            return JSONResponse(json.loads(_WEB_CONFIG_FILE.read_text()))
    except Exception:
        pass
    return JSONResponse({})


@mcp.custom_route("/config", methods=["POST"])
async def save_web_config(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        patch = {k: str(v) for k, v in body.items() if k in _WEB_CONFIG_ALLOWED_KEYS}
        existing: dict = {}
        if _WEB_CONFIG_FILE.exists():
            try:
                existing = json.loads(_WEB_CONFIG_FILE.read_text())
            except Exception:
                pass
        existing.update(patch)
        _WEB_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WEB_CONFIG_FILE.write_text(json.dumps(existing))
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@mcp.custom_route("/config/share", methods=["POST"])
async def create_config_share(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        if not body or not isinstance(body, dict):
            return JSONResponse({"error": "JSON inválido"}, status_code=400)
        share_id = _secrets.token_urlsafe(12)
        _CONFIG_SHARES_DIR.mkdir(parents=True, exist_ok=True)
        (_CONFIG_SHARES_DIR / f"{share_id}.json").write_text(json.dumps(body))
        return JSONResponse({"ok": True, "shareId": share_id})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@mcp.custom_route("/config/share/{share_id}", methods=["GET"])
async def get_config_share(request: Request) -> JSONResponse:
    share_id = request.path_params.get("share_id", "")
    if not share_id or "/" in share_id or ".." in share_id:
        return JSONResponse({"error": "ID inválido"}, status_code=400)
    share_file = _CONFIG_SHARES_DIR / f"{share_id}.json"
    if not share_file.exists():
        return JSONResponse({"error": "No encontrado"}, status_code=404)
    try:
        return JSONResponse(json.loads(share_file.read_text()))
    except Exception:
        return JSONResponse({"error": "Error al leer"}, status_code=500)


_ADJ_DIR = DATA_ROOT / "adj"

def _disk_usage():
    try:
        import shutil
        du = shutil.disk_usage(_ADJ_DIR if _ADJ_DIR.exists() else DATA_ROOT)
        return {"total": du.total, "used": du.used, "free": du.free}
    except Exception:
        return None

@mcp.custom_route("/adj", methods=["GET"])
async def list_adj(request: Request) -> JSONResponse:
    storage = _disk_usage()
    if not _ADJ_DIR.exists():
        return JSONResponse({"files": [], "storage": storage})
    files = []
    for f in sorted(_ADJ_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix == ".json":
            try:
                data = json.loads(f.read_text())
                files.append({"id": f.stem, "data": data, "modified": f.stat().st_mtime, "size": f.stat().st_size})
            except Exception:
                files.append({"id": f.stem, "data": None, "modified": f.stat().st_mtime, "size": f.stat().st_size})
    return JSONResponse({"files": files, "storage": storage})

@mcp.custom_route("/adj", methods=["DELETE"])
async def delete_all_adj(request: Request) -> JSONResponse:
    if not _ADJ_DIR.exists():
        return JSONResponse({"ok": True, "deleted": 0})
    count = 0
    for f in _ADJ_DIR.iterdir():
        if f.suffix == ".json":
            try:
                f.unlink()
                count += 1
            except Exception:
                pass
    return JSONResponse({"ok": True, "deleted": count})

@mcp.custom_route("/adj/{act_id}", methods=["DELETE"])
async def delete_adj(request: Request) -> JSONResponse:
    act_id = request.path_params.get("act_id", "")
    if "/" in act_id or ".." in act_id:
        return JSONResponse({"error": "ID inválido"}, status_code=400)
    adj_file = _ADJ_DIR / f"{act_id}.json"
    if not adj_file.exists():
        return JSONResponse({"error": "No encontrado"}, status_code=404)
    try:
        adj_file.unlink()
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

@mcp.custom_route("/adj/{act_id}", methods=["GET"])
async def get_adj(request: Request) -> JSONResponse:
    act_id = request.path_params.get("act_id", "")
    if "/" in act_id or ".." in act_id:
        return JSONResponse({"error": "ID inválido"}, status_code=400)
    adj_file = _ADJ_DIR / f"{act_id}.json"
    if not adj_file.exists():
        return JSONResponse({})
    try:
        return JSONResponse(json.loads(adj_file.read_text()))
    except Exception:
        return JSONResponse({})

@mcp.custom_route("/adj/{act_id}", methods=["POST"])
async def save_adj(request: Request) -> JSONResponse:
    act_id = request.path_params.get("act_id", "")
    if "/" in act_id or ".." in act_id:
        return JSONResponse({"error": "ID inválido"}, status_code=400)
    try:
        body = await request.json()
        _ADJ_DIR.mkdir(parents=True, exist_ok=True)
        adj_file = _ADJ_DIR / f"{act_id}.json"
        adj_file.write_text(json.dumps(body))
        return JSONResponse({"ok": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@mcp.custom_route("/activities", methods=["GET"])
async def list_activities_web(request: Request) -> JSONResponse:
    import urllib.request as _urllib
    limit = int(request.query_params.get("limit", "30"))
    limit = max(1, min(500, limit))
    start_date = request.query_params.get("start_date", "").strip()  # YYYY-MM-DD
    end_date = request.query_params.get("end_date", "").strip()      # YYYY-MM-DD

    def _normalize(a: dict) -> dict:
        activity_type = a.get("activityType") or {}
        type_key = activity_type.get("typeKey") if isinstance(activity_type, dict) else None
        return {
            "activityId": a.get("activityId"),
            "activityName": a.get("activityName"),
            "startTimeLocal": a.get("startTimeLocal"),
            "activityType": type_key,
            "distanceKm": round((a.get("distance") or 0) / 1000, 2),
            "durationMin": round((a.get("duration") or 0) / 60, 1),
            "avgHr": a.get("averageHR"),
        }

    # Intenta primero con tokens locales
    try:
        with FETCH_LOCK:
            api = _get_api()
            if start_date:
                end = end_date if end_date else date.today().isoformat()
                activities = api.get_activities_by_date(start_date, end, None)
            else:
                activities, _ = _optional_call_first(api, ("get_activities",), 0, limit)
        if activities is not None:
            result = [_normalize(a) for a in activities if isinstance(a, dict)]
            if not start_date:
                result = result[:limit]
            return JSONResponse({"activities": result, "source": "local"})
    except Exception:
        pass

    panel_url = SELF_PUBLIC_URL or "el panel del servidor"
    return JSONResponse({"error": "Sesión de Garmin caducada", "fix": f"Abre {panel_url} en tu navegador y entra en 'Re-loguear Garmin' (tarda 1 minuto)."}, status_code=503)


@mcp.custom_route("/debug/audit", methods=["GET"])
async def debug_audit(_: Request) -> JSONResponse:
    with CACHE_LOCK:
        snapshot = deepcopy(CACHE.get("snapshot"))
        status = CACHE.get("status")
        last_refresh = CACHE.get("last_refresh")
        last_error = CACHE.get("last_error")

    metrics = {}
    if isinstance(snapshot, dict):
        metrics = snapshot.get("metrics") or {}

    keys = [
        "snapshot_obtenido_local",
        "snapshot_obtenido_texto",
        "datos_hasta_local",
        "datos_hasta_texto",
        "predisposicion_para_entrenar",
        "predisposicion_para_entrenar_estado",
        "predisposicion_para_entrenar_texto",
        "body_battery_actual",
        "body_battery_texto",
        "body_battery_resumen_humano",
        "body_battery_nivel_es",
        "estado_vfc",
        "vfc_media_noche_ms",
        "vfc_media_7_dias_ms",
        "estado_vfc_resumen_humano",
        "puntuacion_de_sueno",
        "duracion_de_sueno_texto",
        "sueno_texto_seguro",
        "sueno_resumen_humano",
        "sueno_rem_texto",
        "sueno_profundo_texto",
        "sueno_ligero_texto",
        "sueno_despierto_texto",
        "sueno_inicio_texto",
        "sueno_fin_texto",
        "sueno_fases_resumen_humano",
        "training_readiness_recovery_state",
        "training_readiness_recovery_safe_text",
        "training_readiness_recovery_answer_for_llm",
        "recuperacion_texto_seguro",
    ]

    payload = {
        "status": status,
        "last_refresh": last_refresh,
        "last_refresh_local": _isoish_to_local(last_refresh),
        "last_error": last_error,
        "snapshot_exists": isinstance(snapshot, dict),
        "metrics": {k: metrics.get(k) for k in keys},
    }
    return JSONResponse(payload)



@mcp.custom_route("/debug/activities", methods=["GET"])
async def debug_activities(_: Request) -> JSONResponse:
    with FETCH_LOCK:
        api = _get_api()
        activities, err = _optional_call_first(api, ("get_activities",), 0, 5)
        if err:
            return JSONResponse({"error": str(err)}, status_code=500)
        if not activities:
            return JSONResponse({"error": "No activities found"}, status_code=404)
        
        result = [
            {
                "activityId": a.get("activityId"),
                "activityName": a.get("activityName"),
                "startTimeLocal": a.get("startTimeLocal"),
                "activityType": a.get("activityType", {}).get("typeKey") if isinstance(a.get("activityType"), dict) else None,
            }
            for a in activities[:5]
        ]
        return JSONResponse({"activities": result})
    # === End debug_activities ===

    raw_sleep_top_level = {
        k: v for k, v in raw_sources.items()
        if should_keep(k)
    }

    raw_sleep_candidates = walk(raw_sources)

    normalized_sleep = {
        "snapshot_obtenido_local": metrics.get("snapshot_obtenido_local"),
        "snapshot_obtenido_texto": metrics.get("snapshot_obtenido_texto"),
        "datos_hasta_local": metrics.get("datos_hasta_local"),
        "datos_hasta_texto": metrics.get("datos_hasta_texto"),
        "puntuacion_de_sueno": metrics.get("puntuacion_de_sueno"),
        "duracion_de_sueno_texto": metrics.get("duracion_de_sueno_texto"),
        "sueno_texto_seguro": metrics.get("sueno_texto_seguro"),
        "sueno_resumen_humano": metrics.get("sueno_resumen_humano"),
        "sueno_rem_texto": metrics.get("sueno_rem_texto"),
        "sueno_profundo_texto": metrics.get("sueno_profundo_texto"),
        "sueno_ligero_texto": metrics.get("sueno_ligero_texto"),
        "sueno_despierto_texto": metrics.get("sueno_despierto_texto"),
        "sueno_inicio_texto": metrics.get("sueno_inicio_texto"),
        "sueno_fin_texto": metrics.get("sueno_fin_texto"),
        "sueno_fases_resumen_humano": metrics.get("sueno_fases_resumen_humano"),
        "sleep_score": metrics.get("sleep_score"),
        "sleep_duration_seconds": metrics.get("sleep_duration_seconds"),
    }

    payload = {
        "status": status,
        "last_refresh": last_refresh,
        "last_refresh_local": _isoish_to_local(last_refresh),
        "last_error": last_error,
        "snapshot_exists": isinstance(snapshot, dict),
        "normalized_sleep_metrics": normalized_sleep,
        "raw_sources_info": raw_sources_info,
        "raw_sleep_top_level": raw_sleep_top_level,
        "raw_sleep_candidates": raw_sleep_candidates,
    }
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Re-login web wizard routes
# ---------------------------------------------------------------------------


def _login_unauthorized_html() -> str:
    body = (
        '<h1>404</h1>'
        '<p class="muted">Página no encontrada.</p>'
    )
    return _login_render_page("404", None, body)


@mcp.custom_route("/login", methods=["GET"])
async def login_start(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    if not _login_admin_ok(request):
        return HTMLResponse(_login_unauthorized_html(), status_code=404)

    _login_cleanup_expired_sessions()

    body = (
        '<h1>Re-login Garmin</h1>'
        '<p class="muted">Vamos a renovar los tokens. Necesitas tu email, contraseña y el código MFA que llegará al Gmail.</p>'
        '<form method="POST" action="/login/submit">'
        '<label for="email">Email Garmin</label>'
        '<input id="email" name="email" type="email" autocomplete="username" required autofocus>'
        '<label for="password">Contraseña</label>'
        + _password_input_html("password", "password", autocomplete="current-password") +
        '<button type="submit">Siguiente</button>'
        '</form>'
    )
    response = HTMLResponse(_login_render_page("Re-login Garmin · paso 1", 1, body))
    active_token = _login_active_token(request)
    if active_token:
        _set_auth_cookie(response, active_token, request)
    return response


@mcp.custom_route("/login/submit", methods=["POST"])
async def login_submit(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    if not _login_admin_ok(request):
        return HTMLResponse(_login_unauthorized_html(), status_code=404)

    form = await request.form()
    email = (form.get("email") or "").strip()
    password = (form.get("password") or "").strip()
    if not email or not password:
        body = (
            '<h1>Faltan datos</h1>'
            '<div class="error">Email o contraseña vacíos.</div>'
            '<a href="/login">← Volver</a>'
        )
        return HTMLResponse(_login_render_page("Error", 1, body), status_code=400)

    session_id = _secrets.token_urlsafe(18)
    mfa_event = threading.Event()
    mfa_holder: dict[str, Any] = {"code": None}
    session = {
        "status": "starting",
        "error": None,
        "tokens": None,
        "persist_report": None,
        "mfa_event": mfa_event,
        "mfa_holder": mfa_holder,
        "created_at": time.time(),
    }
    _login_set_session(session_id, session)

    thread = threading.Thread(
        target=_login_worker,
        args=(session_id, email, password),
        daemon=True,
        name=f"login-{session_id[:6]}",
    )
    session["thread"] = thread
    thread.start()

    return RedirectResponse(f"/login/wait?session={session_id}", status_code=303)


@mcp.custom_route("/login/wait", methods=["GET"])
async def login_wait(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    # Authorization here is the session id itself (a 144-bit secret created by an
    # authorized /login/submit). The admin/bootstrap token may already be gone by
    # the time we reach the later steps, so we must NOT gate on it.
    session_id = request.query_params.get("session", "").strip()
    session = _login_get_session(session_id)
    if not session:
        body = (
            '<h1>Sesión caducada</h1>'
            '<p class="muted">Esta sesión de login expiró. Vuelve a empezar.</p>'
            '<a href="/login">← Empezar de nuevo</a>'
        )
        return HTMLResponse(_login_render_page("Sesión caducada", None, body), status_code=410)

    status = session.get("status", "starting")
    if status == "awaiting_mfa":
        return RedirectResponse(f"/login/mfa?session={session_id}", status_code=303)
    if status in ("success", "error"):
        return RedirectResponse(f"/login/result?session={session_id}", status_code=303)

    body = (
        '<h2>Conectando con Garmin…</h2>'
        '<p class="muted">Estamos solicitando el código MFA. Revisa tu Gmail en unos segundos.</p>'
        '<div class="spinner" aria-hidden="true"></div>'
        '<p class="muted" style="text-align:center;font-size:13px">Esta página se actualizará sola.</p>'
    )
    return HTMLResponse(
        _login_render_page("Procesando", 1, body, auto_refresh_seconds=2)
    )


@mcp.custom_route("/login/mfa", methods=["GET"])
async def login_mfa_form(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    # Authorized by the session id (see note in login_wait).
    session_id = request.query_params.get("session", "").strip()
    session = _login_get_session(session_id)
    if not session:
        body = (
            '<h1>Sesión caducada</h1>'
            '<p class="muted">Esta sesión expiró.</p>'
            '<a href="/login">← Empezar de nuevo</a>'
        )
        return HTMLResponse(_login_render_page("Sesión caducada", None, body), status_code=410)

    status = session.get("status", "starting")
    if status in ("success", "error"):
        return RedirectResponse(f"/login/result?session={session_id}", status_code=303)
    if status != "awaiting_mfa":
        return RedirectResponse(f"/login/wait?session={session_id}", status_code=303)

    sid_safe = _html.escape(session_id)
    body = (
        '<h2>Código de verificación</h2>'
        '<p class="muted">Hemos pedido a Garmin que te envíe un código de 6 dígitos al Gmail. Revisa la bandeja de entrada y pégalo aquí.</p>'
        '<form method="POST" action="/login/mfa">'
        f'<input type="hidden" name="session" value="{sid_safe}">'
        '<label for="code">Código MFA</label>'
        '<input id="code" name="code" type="text" inputmode="numeric" pattern="[0-9]*" '
        'autocomplete="one-time-code" maxlength="10" required autofocus>'
        '<button type="submit">Siguiente</button>'
        '</form>'
    )
    return HTMLResponse(_login_render_page("Re-login Garmin · paso 2", 2, body))


@mcp.custom_route("/login/mfa", methods=["POST"])
async def login_mfa_submit(request: Request) -> Response:
    from starlette.responses import HTMLResponse, RedirectResponse
    # Authorized by the session id carried in the form (see note in login_wait).
    form = await request.form()
    session_id = (form.get("session") or "").strip()
    code = (form.get("code") or "").strip()
    session = _login_get_session(session_id)
    if not session:
        body = (
            '<h1>Sesión caducada</h1>'
            '<p class="muted">Esta sesión expiró.</p>'
            '<a href="/login">← Empezar de nuevo</a>'
        )
        return HTMLResponse(_login_render_page("Sesión caducada", None, body), status_code=410)
    if not code:
        return RedirectResponse(f"/login/mfa?session={session_id}", status_code=303)

    session["mfa_holder"]["code"] = code
    session["mfa_event"].set()
    return RedirectResponse(f"/login/result?session={session_id}", status_code=303)


@mcp.custom_route("/login/result", methods=["GET"])
async def login_result(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    # Authorized by the session id (see note in login_wait). The bootstrap token is
    # cleared the instant login succeeds, so gating on it here would 404 the very
    # screen that confirms success.
    session_id = request.query_params.get("session", "").strip()
    session = _login_get_session(session_id)
    if not session:
        # Most likely: setup already completed (we drop the session after showing
        # the result). Show a friendly confirmation instead of a scary error.
        if not _is_first_run():
            body = (
                '<h1>✅ Configuración completada</h1>'
                '<p>Tu cuenta Garmin ya está conectada.</p>'
                '<script>setTimeout(function(){window.location.href="/admin"},2000)</script>'
                '<a href="/admin"><button type="button">Ir al panel</button></a>'
            )
            return HTMLResponse(_login_render_page("Completado", None, body))
        body = (
            '<h1>Sesión caducada</h1>'
            '<p class="muted">Esta sesión expiró o ya se cerró.</p>'
            '<a href="/login">← Empezar de nuevo</a>'
        )
        return HTMLResponse(_login_render_page("Sesión caducada", None, body), status_code=410)

    status = session.get("status", "starting")

    if status not in ("success", "error"):
        body = (
            '<h2>Procesando login…</h2>'
            '<p class="muted">Validando el código MFA con Garmin. Aguanta un momento.</p>'
            '<div class="spinner" aria-hidden="true"></div>'
        )
        return HTMLResponse(
            _login_render_page("Procesando", 3, body, auto_refresh_seconds=2)
        )

    if status == "error":
        err = _html.escape(session.get("error") or "Error desconocido")
        body = (
            '<h1>Algo falló</h1>'
            f'<div class="error">{err}</div>'
            '<p class="muted">Las causas más típicas: contraseña incorrecta, código MFA caducado, o Garmin pidiendo CAPTCHA. Prueba de nuevo.</p>'
            '<a href="/login"><button type="button">Empezar de nuevo</button></a>'
        )
        _login_drop_session(session_id)
        return HTMLResponse(_login_render_page("Error", 3, body), status_code=400)

    # success
    report = session.get("persist_report") or {}
    tokens_b64 = report.get("tokens_b64") or ""

    public_url = _public_base_url(request)
    mcp_url = f"{public_url}/mcp"

    parts = ['<h1>¡Listo!</h1>']

    if report.get("disk", {}).get("ok"):
        parts.append(
            '<div class="success">Tokens guardados en el volumen persistente. '
            'Tu MCP aguanta reinicios del servidor.</div>'
        )
    else:
        # Manual persistence path: show ONE copy-paste block with the tokens.
        env_block = f"GARMIN_TOKENS_JSON={tokens_b64}"
        parts.append(
            '<p>Tu MCP está activo <strong>durante esta sesión</strong>. Para hacerlo permanente, '
            'define la variable de entorno <code>GARMIN_TOKENS_JSON</code> de tu servidor con este valor:</p>'
            f'<pre style="max-height:260px;overflow:auto;background:#1c1c22;color:#f2f2f7;padding:14px;border-radius:10px;font-size:13px;line-height:1.5;word-break:break-all;white-space:pre-wrap;border:1px solid #2a2a32" id="env-block">{_html.escape(env_block)}</pre>'
            '<button type="button" onclick="navigator.clipboard.writeText(document.getElementById(\'env-block\').innerText);this.innerText=\'Copiado ✓\'">Copiar bloque</button>'
            '<a href="/admin" style="display:inline-block;margin-top:16px"><button type="button" class="secondary">Ir al panel</button></a>'
        )

    parts.append('<h2 style="margin-top:32px">Conéctalo a tu IA</h2>')
    parts.append('<p class="muted">Funciona con cualquier IA con conectores MCP (Claude, ChatGPT…). Pega esta URL en Settings → Connectors:</p>')
    parts.append(
        f'<pre id="mcp-url">{_html.escape(mcp_url)}</pre>'
        '<button type="button" onclick="navigator.clipboard.writeText(document.getElementById(\'mcp-url\').innerText);this.innerText=\'Copiado ✓\'">Copiar URL del conector</button>'
    )

    _login_drop_session(session_id)
    return HTMLResponse(_login_render_page("Listo", 3, "".join(parts)))


# ---------------------------------------------------------------------------
# Guided setup wizards: password lock
# ---------------------------------------------------------------------------


@mcp.custom_route("/setup/proteccion", methods=["GET"])
async def setup_protection_form(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    if not _login_admin_ok(request):
        return HTMLResponse(_login_unauthorized_html(), status_code=404)

    already = bool(_current_admin_token())
    title = "Cambiar contraseña" if already else "Protección con contraseña"
    intro = (
        'Escribe la nueva contraseña que quieras. La anterior dejará de funcionar.'
        if already else
        'Escribe la contraseña que tú quieras. A partir de ahora, esta página de configuración '
        'solo se abrirá con ella, para que nadie más pueda tocar tu cuenta de Garmin.'
    )
    submit = "Guardar nueva contraseña" if already else "Activar protección"
    body = (
        f'<h1>{title}</h1>'
        f'<p>{intro}</p>'
        '<form method="POST" action="/setup/proteccion">'
        '<label for="pwd">Tu contraseña (mínimo 8 caracteres)</label>'
        + _password_input_html("pwd", "pwd", placeholder="la que tú quieras",
                               autocomplete="new-password", minlength=8, autofocus=True) +
        '<button type="submit">' + submit + '</button>'
        '<button type="button" class="secondary" onclick="'
        "var p=Array.from(crypto.getRandomValues(new Uint8Array(18))).map(b=>('0'+b.toString(16)).slice(-2)).join('');"
        "document.getElementById('pwd').value=p;"
        '">O genérame una segura al azar</button>'
        '</form>'
        '<a href="/admin"><button type="button" class="secondary">← Volver al panel</button></a>'
    )
    return HTMLResponse(_login_render_page(title, None, body))


@mcp.custom_route("/setup/proteccion", methods=["POST"])
async def setup_protection_submit(request: Request) -> Response:
    from starlette.responses import HTMLResponse
    if not _login_admin_ok(request):
        return HTMLResponse(_login_unauthorized_html(), status_code=404)

    form = await request.form()
    pwd = (form.get("pwd") or "").strip()

    def _err(msg: str) -> Response:
        body = (
            '<h1>No se pudo activar</h1>'
            f'<div class="error">{_html.escape(msg)}</div>'
            '<a href="/setup/proteccion"><button type="button">Volver a intentar</button></a>'
            '<a href="/admin"><button type="button" class="secondary">← Volver al panel</button></a>'
        )
        return HTMLResponse(_login_render_page("Error", None, body), status_code=400)

    if len(pwd) < 8:
        return _err("La contraseña debe tener al menos 8 caracteres.")

    changed = bool(_current_admin_token())

    # 1. Apply immediately (no restart needed) by writing the live file.
    try:
        _write_admin_token_file(pwd)
    except Exception as exc:  # noqa: BLE001
        return _err(f"No se pudo guardar la contraseña: {exc}")

    heading = "✅ Contraseña actualizada" if changed else "✅ Protección activada"
    public_url = _public_base_url(request)
    login_url = f"{public_url}/login?token={pwd}"

    persist_note = (
        '<div class="success">Ya está activa y guardada de forma permanente '
        '(se conserva en el volumen de datos).</div>'
    )

    body = (
        f'<h1>{heading}</h1>'
        f'{persist_note}'
        '<p class="muted" style="margin-top:8px">Tu contraseña es: '
        '<span id="pwd-display" data-pwd="' + _html.escape(pwd, quote=True) + '" '
        'style="font-family:monospace;background:rgba(255,255,255,.1);padding:2px 6px;border-radius:5px">'
        '********</span>'
        '<button type="button" id="pwd-toggle" style="background:none;border:none;color:#9a9aa2;cursor:pointer;font-size:18px;padding:2px 6px;vertical-align:middle;width:auto;margin:0" onclick="'
        "var s=document.getElementById('pwd-display');"
        "if(s.dataset.shown){s.textContent='********';s.dataset.shown='';this.textContent='👁';}"
        "else{s.textContent=s.dataset.pwd;s.dataset.shown='1';this.textContent='🙈';}"
        '">👁</button></p>'
        '<h2 style="margin-top:24px">Guarda este enlace (opcional)</h2>'
        '<p class="muted">Atajo para entrar sin teclear la contraseña. Guárdalo en favoritos del móvil:</p>'
        f'<pre id="login-url">{_html.escape(login_url)}</pre>'
        '<button type="button" onclick="navigator.clipboard.writeText('
        "document.getElementById('login-url').innerText);this.innerText='Copiado ✓'"
        '">Copiar enlace</button>'
        '<a href="/admin"><button type="button" class="secondary" style="margin-top:20px">← Volver al panel</button></a>'
    )
    resp = HTMLResponse(_login_render_page("Listo", None, body))
    # Keep the current browser logged in with the new password.
    _set_auth_cookie(resp, pwd, request)
    return resp



@mcp.tool
def get_cached_snapshot() -> dict[str, Any]:
    """Última foto cacheada del día actual, con métricas normalizadas y raw_sources."""
    with CACHE_LOCK:
        snapshot = deepcopy(CACHE["snapshot"])
        metrics = {}
        if isinstance(snapshot, dict):
            metrics = snapshot.get("metrics") or {}

        return {
            "status": CACHE["status"],
            "last_refresh": CACHE["last_refresh"],
            "last_refresh_local": _isoish_to_local(CACHE["last_refresh"]),
            "last_error": CACHE["last_error"],
            "snapshot_obtenido_local": _isoish_to_local(snapshot.get("fetched_at")) if isinstance(snapshot, dict) else None,
            "datos_hasta_local": metrics.get("datos_hasta_local") if isinstance(metrics, dict) else None,
            "ultima_sincronizacion_conector_local": _isoish_to_local(CACHE["last_refresh"]),
            "snapshot": snapshot,
        }


@mcp.tool
def refresh_snapshot() -> dict[str, Any]:
    """Fuerza una actualización inmediata desde Garmin."""
    return _refresh_cache_sync()


@mcp.tool
def get_day_snapshot(target_date: str | None = None) -> dict[str, Any]:
    """Foto completa de un día concreto (YYYY-MM-DD)."""
    return _collect_day_snapshot(_parse_date(target_date), include_recent_activities=False)


@mcp.tool
def get_raw_sources(target_date: str | None = None, include_recent_activities: bool = True) -> dict[str, Any]:
    """Devuelve los payloads crudos que ha devuelto Garmin para un día."""
    snapshot = _collect_day_snapshot(_parse_date(target_date), include_recent_activities=include_recent_activities)
    return {
        "date": snapshot["date"],
        "fetched_at": snapshot["fetched_at"],
        "raw_sources": snapshot["raw_sources"],
        "source_errors": snapshot["source_errors"],
    }


@mcp.tool
def get_primary_device_info(target_date: str | None = None) -> dict[str, Any]:
    """Devuelve el dispositivo principal detectado y, si existe, su configuración."""
    snapshot = _collect_day_snapshot(_parse_date(target_date), include_recent_activities=False)
    raw = snapshot["raw_sources"]
    return {
        "date": snapshot["date"],
        "primary_device_info_raw": raw.get("primary_device_info_raw"),
        "devices_raw": raw.get("devices_raw"),
        "device_settings_raw": raw.get("device_settings_raw"),
        "source_errors": snapshot["source_errors"],
    }


@mcp.tool
def get_recent_activities(limit: int = 100) -> list[dict[str, Any]]:
    """Actividades recientes normalizadas. Por defecto hasta 100.
    Usa limit=-1 para obtener TODAS las actividades (historico completo desde 2016).
    Advertencia: obtener todo puede tardar varios minutos."""
    if limit < 0:
        limit = 9999
    limit = max(1, min(200, int(limit)))
    with FETCH_LOCK:
        api = _get_api()
        activities, err = _optional_call_first(api, ("get_activities",), 0, limit)
        if activities is None:
            raise RuntimeError(err or "No pude leer las actividades recientes")
        return [_normalize_activity(a) for a in activities[:limit] if isinstance(a, dict)]


@mcp.tool
def get_activity_fit_download(activity_id: str) -> str:
    """Link de descarga del .fit/.zip de una actividad.
    Solo decí "dame el fit de [nombre o ID]" y te da el link para descargar.
    Ejemplo: get_activity_fit_download("22621731390")
    Returns clickable URL."""
    base = SELF_PUBLIC_URL
    if base:
        return f"👉 Descarga el .fit aquí: {base}/download/{activity_id}"
    return f"Ruta de descarga: /download/{activity_id} (añade el dominio público de tu servidor)"


@mcp.tool
def get_window_rollup(days: int = 7) -> list[dict[str, Any]]:
    """Rollup de varios días hacia atrás."""
    days = max(1, min(7, int(days)))
    end = _today_local()
    results = []
    for offset in range(days - 1, -1, -1):
        target = (end - timedelta(days=offset)).isoformat()
        results.append(_collect_day_snapshot(target, include_recent_activities=False))
        time.sleep(0.4)
    return results

class _UserAuthMiddleware:
    """ASGI middleware that authenticates MCP requests via X-User-API-Key header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                headers = dict(scope.get("headers", []))
                api_key = headers.get(b"x-user-api-key", b"").decode("utf-8", errors="replace").strip()
                if not api_key:
                    import urllib.parse
                    qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
                    params = urllib.parse.parse_qs(qs)
                    api_key = params.get("api_key", [""])[0].strip()
                if api_key:
                    user = _get_user_by_api_key(api_key)
                    if user:
                        token = current_user.set(user)
                        try:
                            await self.app(scope, receive, send)
                        finally:
                            current_user.reset(token)
                        return
        await self.app(scope, receive, send)


class _MCPHitTracker:
    """Starlette ASGI middleware that records the last hit to /mcp* paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                global _LAST_MCP_HIT, _LAST_MCP_CLIENT
                with _LAST_MCP_HIT_LOCK:
                    _LAST_MCP_HIT = time.time()
                    headers = dict(scope.get("headers", []))
                    ua = headers.get(b"user-agent", b"").decode("utf-8", errors="replace")
                    if ua:
                        _LAST_MCP_CLIENT = ua
        await self.app(scope, receive, send)


def _run_server() -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(target=_background_refresh_loop, daemon=True)
    thread.start()

    # First-run banner so the user finds the setup wizard quickly from logs.
    if _is_first_run():
        print("=" * 60, flush=True)
        print("Garmin Coach MCP — PRIMER ARRANQUE", flush=True)
        print("Abre la URL pública de tu servicio en el navegador y pulsa", flush=True)
        print("'Conectar' para configurar tu cuenta de Garmin.", flush=True)
        print("=" * 60, flush=True)

    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=PORT,
        stateless_http=True,
        middleware=[
            Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
            Middleware(_UserAuthMiddleware),
            Middleware(_MCPHitTracker),
        ],
    )


# === BEGIN GARMIN METRICS PATCH ===

_GARMIN_PATCH_STRESS_LABEL_ES = {
    "REST": "Descanso",
    "LOW": "Bajo",
    "MEDIUM": "Medio",
    "HIGH": "Alto",
    "BALANCED": "Equilibrado",
}

_GARMIN_PATCH_HRV_STATUS_ES = {
    "BALANCED": "Equilibrado",
    "UNBALANCED": "Desequilibrado",
    "LOW": "Bajo",
    "POOR": "Bajo",
}

_GARMIN_PATCH_TRAINING_READINESS_STATUS_ES = {
    "LOW": "Bajo",
    "MODERATE": "Moderada",
    "HIGH": "Alto",
}

_GARMIN_PATCH_TRAINING_READINESS_MESSAGE_ES = {
    "WORKING_HARD": "Entrenando duro",
    "BALANCE_YOUR_TRAINING_LOAD": "Equilibra tu carga de entrenamiento",
}

_GARMIN_PATCH_ACUTE_LOAD_STATUS_ES = {
    "OPTIMAL": "Óptimo",
    "LOW": "Baja",
    "HIGH": "Alta",
}


def _garmin_patch_first_non_none(*values):
    for v in values:
        if v is not None:
            return v
    return None


def _garmin_patch_put(metrics, key, value):
    if value is not None:
        metrics[key] = value


def _garmin_patch_minutes(seconds):
    if seconds is None:
        return None
    try:
        return int(round(float(seconds) / 60.0))
    except Exception:
        return None


def _garmin_patch_pick_training_readiness(raw_value):
    if isinstance(raw_value, dict):
        entries = [raw_value]
    elif isinstance(raw_value, list):
        entries = [x for x in raw_value if isinstance(x, dict)]
    else:
        entries = []

    if not entries:
        return None

    def rank(entry):
        ts = str(entry.get("timestampLocal") or entry.get("timestamp") or "")
        return (
            1 if entry.get("validSleep") else 0,
            1 if entry.get("inputContext") == "UPDATE_REALTIME_VARIABLES" else 0,
            ts,
        )

    return sorted(entries, key=rank, reverse=True)[0]


_GARMIN_PATCH_ORIGINAL_COLLECT_DAY_SNAPSHOT = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _GARMIN_PATCH_ORIGINAL_COLLECT_DAY_SNAPSHOT(*args, **kwargs)

    raw = snap.get("raw_sources") or {}
    metrics = snap.get("metrics") or {}
    snap["metrics"] = metrics

    summary = raw.get("summary_raw") or {}
    heart = raw.get("heart_raw") or {}
    sleep = raw.get("sleep_raw") or {}
    stress = raw.get("stress_raw") or {}
    hrv = raw.get("hrv_raw") or {}
    training_readiness = _garmin_patch_pick_training_readiness(raw.get("training_readiness_raw"))
    training_status = raw.get("training_status_raw") or {}
    user_profile = raw.get("user_profile_raw") or {}

    _garmin_patch_put(metrics, "body_battery_current", summary.get("bodyBatteryMostRecentValue"))
    _garmin_patch_put(metrics, "body_battery_max", summary.get("bodyBatteryHighestValue"))
    _garmin_patch_put(metrics, "body_battery_min", summary.get("bodyBatteryLowestValue"))

    sleep_dto = sleep.get("dailySleepDTO") or {}
    sleep_seconds = _garmin_patch_first_non_none(
        sleep_dto.get("sleepTimeSeconds"),
        summary.get("sleepingSeconds"),
    )
    _garmin_patch_put(metrics, "sleep_duration_seconds", sleep_seconds)
    _garmin_patch_put(metrics, "sleep_hours", round(sleep_seconds / 3600, 1) if sleep_seconds is not None else None)
    _garmin_patch_put(metrics, "sleep_score", ((sleep_dto.get("sleepScores") or {}).get("overall") or {}).get("value"))
    _garmin_patch_put(metrics, "sleep_deep_min", _garmin_patch_minutes(sleep_dto.get("deepSleepSeconds")))
    _garmin_patch_put(metrics, "sleep_rem_min", _garmin_patch_minutes(sleep_dto.get("remSleepSeconds")))
    _garmin_patch_put(metrics, "sleep_light_min", _garmin_patch_minutes(sleep_dto.get("lightSleepSeconds")))
    _garmin_patch_put(metrics, "sleep_awake_min", _garmin_patch_minutes(sleep_dto.get("awakeSleepSeconds")))

    _garmin_patch_put(
        metrics,
        "resting_heart_rate",
        _garmin_patch_first_non_none(
            heart.get("restingHeartRate"),
            summary.get("restingHeartRate"),
            sleep.get("restingHeartRate"),
        ),
    )
    _garmin_patch_put(
        metrics,
        "resting_heart_rate_7d_avg",
        _garmin_patch_first_non_none(
            heart.get("lastSevenDaysAvgRestingHeartRate"),
            summary.get("lastSevenDaysAvgRestingHeartRate"),
        ),
    )

    stress_label = _garmin_patch_first_non_none(
        summary.get("stressQualifier"),
        stress.get("stressQualifier"),
    )
    _garmin_patch_put(metrics, "stress_avg", _garmin_patch_first_non_none(summary.get("averageStressLevel"), stress.get("avgStressLevel")))
    _garmin_patch_put(metrics, "stress_max", _garmin_patch_first_non_none(summary.get("maxStressLevel"), stress.get("maxStressLevel")))
    _garmin_patch_put(metrics, "stress_label", stress_label)
    if stress_label is not None:
        metrics["stress_label_es"] = _GARMIN_PATCH_STRESS_LABEL_ES.get(stress_label, metrics.get("stress_label_es"))

    hrv_summary = hrv.get("hrvSummary") or {}
    hrv_baseline = hrv_summary.get("baseline") or {}
    hrv_status = hrv_summary.get("status")
    _garmin_patch_put(metrics, "hrv_last_night", hrv_summary.get("lastNightAvg"))
    _garmin_patch_put(metrics, "hrv_weekly_avg", hrv_summary.get("weeklyAvg"))
    _garmin_patch_put(metrics, "hrv_status", hrv_status)
    _garmin_patch_put(metrics, "hrv_baseline_low", hrv_baseline.get("balancedLow"))
    _garmin_patch_put(metrics, "hrv_baseline_high", hrv_baseline.get("balancedUpper"))
    _garmin_patch_put(metrics, "hrv_last_night_5min_high", hrv_summary.get("lastNight5MinHigh"))
    if hrv_status is not None:
        metrics["hrv_status_es"] = _GARMIN_PATCH_HRV_STATUS_ES.get(hrv_status, metrics.get("hrv_status_es"))

    if training_readiness:
        tr_status = training_readiness.get("level")
        tr_message = training_readiness.get("feedbackShort")
        _garmin_patch_put(metrics, "training_readiness_score", training_readiness.get("score"))
        _garmin_patch_put(metrics, "training_readiness_status", tr_status)
        _garmin_patch_put(metrics, "training_readiness_message", tr_message)
        _garmin_patch_put(metrics, "training_readiness_recovery_time", training_readiness.get("recoveryTime"))
        _garmin_patch_put(metrics, "training_readiness_input_context", training_readiness.get("inputContext"))
        if tr_status is not None:
            metrics["training_readiness_status_es"] = _GARMIN_PATCH_TRAINING_READINESS_STATUS_ES.get(
                tr_status,
                metrics.get("training_readiness_status_es"),
            )
        if tr_message is not None:
            metrics["training_readiness_message_es"] = _GARMIN_PATCH_TRAINING_READINESS_MESSAGE_ES.get(
                tr_message,
                metrics.get("training_readiness_message_es"),
            )

    latest_status_data = (((training_status.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")) or {})
    acute = None
    if isinstance(latest_status_data, dict):
        for device_data in latest_status_data.values():
            if isinstance(device_data, dict):
                acute = device_data.get("acuteTrainingLoadDTO")
                if acute:
                    break

    acute_status = None
    if acute:
        acute_status = acute.get("acwrStatus")
        _garmin_patch_put(metrics, "acute_load", acute.get("dailyTrainingLoadAcute"))
        _garmin_patch_put(metrics, "acute_load_ratio", acute.get("dailyAcuteChronicWorkloadRatio"))
        _garmin_patch_put(metrics, "acute_load_status", acute_status)
        if acute_status is not None:
            metrics["acute_load_status_es"] = _GARMIN_PATCH_ACUTE_LOAD_STATUS_ES.get(
                acute_status,
                metrics.get("acute_load_status_es"),
            )

    _garmin_patch_put(metrics, "steps", summary.get("totalSteps"))
    _garmin_patch_put(metrics, "steps_goal", summary.get("dailyStepGoal"))

    vo2_block = ((training_status.get("mostRecentVO2Max") or {}).get("generic")) or {}
    profile_data = (user_profile.get("userData") or {})

    # Fitness age (Edad Física) – puede venir de fitness_age_raw o de vo2_block
    fitness_age_raw = raw.get("fitness_age_raw") or {}
    fitness_age_val = (
        vo2_block.get("fitnessAge")
        or (fitness_age_raw.get("fitnessAge") if isinstance(fitness_age_raw, dict) else None)
        or (fitness_age_raw.get("value") if isinstance(fitness_age_raw, dict) else None)
    )
    if fitness_age_val is not None:
        _garmin_patch_put(metrics, "fitness_age", fitness_age_val)

    _garmin_patch_put(
        metrics,
        "vo2max",
        _garmin_patch_first_non_none(
            vo2_block.get("vo2MaxPreciseValue"),
            vo2_block.get("vo2MaxValue"),
            profile_data.get("vo2MaxRunning"),
        ),
    )

    # VO2 max label (maxMetCategory: 0=Deficiente,1=Bajo,2=Aceptable,3=Bueno,4=Excelente,5=Superior)
    _VO2MAX_CAT_ES = {0: "Deficiente", 1: "Bajo", 2: "Aceptable", 3: "Bueno", 4: "Excelente", 5: "Superior"}
    vo2_cat = vo2_block.get("maxMetCategory")
    if vo2_cat is not None:
        _garmin_patch_put(metrics, "vo2max_label", _VO2MAX_CAT_ES.get(vo2_cat))

    # Respiración
    respiration = raw.get("respiration_raw") or {}
    _garmin_patch_put(metrics, "respiration_waking_avg", respiration.get("avgWakingRespirationValue"))
    _garmin_patch_put(metrics, "respiration_sleep_avg", respiration.get("avgSleepRespirationValue"))
    _garmin_patch_put(metrics, "respiration_min", respiration.get("lowestRespirationValue"))
    _garmin_patch_put(metrics, "respiration_max", respiration.get("highestRespirationValue"))

    # SpO2
    spo2 = raw.get("spo2_raw") or {}
    if isinstance(spo2, dict):
        _garmin_patch_put(metrics, "spo2_latest", spo2.get("latestSpO2"))
        _garmin_patch_put(metrics, "spo2_avg_day", spo2.get("averageSpO2"))
        _garmin_patch_put(metrics, "spo2_avg_sleep", spo2.get("avgSleepSpO2"))
        _garmin_patch_put(metrics, "spo2_min", spo2.get("lowestSpO2"))
        _garmin_patch_put(metrics, "spo2_7d_avg", spo2.get("lastSevenDaysAvgSpO2"))

    return snap

# === END GARMIN METRICS PATCH ===

# === GARMIN_ES_TRANSLATIONS_PATCH_START ===
_GARMIN_STATUS_ES_GENERIC = {
    "BALANCED": "Equilibrado",
    "LOW": "Bajo",
    "MODERATE": "Moderada",
    "HIGH": "Alto",
    "OPTIMAL": "Óptimo",
    "POOR": "Deficiente",
    "UNBALANCED": "Desequilibrado",
    "NORMAL": "Normal",
}

_GARMIN_STATUS_ES_BY_FIELD = {
    "stress_label": {
        "BALANCED": "Equilibrado",
        "LOW": "Bajo",
        "MODERATE": "Moderada",
        "HIGH": "Alto",
    },
    "hrv_status": {
        "BALANCED": "Equilibrada",
        "LOW": "Baja",
        "MODERATE": "Moderada",
        "HIGH": "Alta",
    },
    "training_readiness_status": {
        "BALANCED": "Equilibrada",
        "LOW": "Baja",
        "MODERATE": "Moderada",
        "HIGH": "Alta",
    },
    "acute_load_status": {
        "OPTIMAL": "Óptima",
        "LOW": "Baja",
        "MODERATE": "Moderada",
        "HIGH": "Alta",
        "BALANCED": "Equilibrada",
        "POOR": "Deficiente",
        "UNBALANCED": "Desequilibrada",
    },
}

_GARMIN_TRAINING_READINESS_MESSAGE_ES = {
    "WORKING_HARD": "Entrenando duro",
    "BALANCE_YOUR_TRAINING_LOAD": "Equilibra tu carga de entrenamiento",
    "READY_TO_TRAIN": "Listo para entrenar",
    "RECOVERING": "Recuperando",
    "WELL_RECOVERED": "Bien recuperado",
    "FATIGUED": "Fatigado",
}

_GARMIN_TRAINING_STATUS_ES = {
    "PRODUCTIVE": "Productivo",
    "MAINTAINING": "Mantenimiento",
    "RECOVERY": "Recuperación",
    "PEAKING": "Pico de forma",
    "UNPRODUCTIVE": "No productivo",
    "OVERREACHING": "Sobrecarga",
    "DETRAINING": "Desentrenamiento",
    "NO_STATUS": "Sin estado",
}

def _translate_metric_status_es(field_name, value):
    if not value or not isinstance(value, str):
        return None
    field_map = _GARMIN_STATUS_ES_BY_FIELD.get(field_name) or {}
    return field_map.get(value) or _GARMIN_STATUS_ES_GENERIC.get(value)

def _translate_training_readiness_message_es(value):
    if not value or not isinstance(value, str):
        return None
    return _GARMIN_TRAINING_READINESS_MESSAGE_ES.get(value)

def _translate_training_status_es(value):
    if not value or not isinstance(value, str):
        return None
    base = value.split("_", 1)[0]
    return _GARMIN_TRAINING_STATUS_ES.get(base)

def _extract_training_status_code(raw):
    if not isinstance(raw, dict):
        return None

    latest = ((raw.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {})
    if not isinstance(latest, dict) or not latest:
        return None

    entry = None
    for v in latest.values():
        if isinstance(v, dict) and v.get("primaryTrainingDevice"):
            entry = v
            break

    if entry is None:
        entry = next((v for v in latest.values() if isinstance(v, dict)), None)

    if not isinstance(entry, dict):
        return None

    phrase = entry.get("trainingStatusFeedbackPhrase")
    if isinstance(phrase, str) and phrase:
        return phrase.split("_", 1)[0]

    return None

if "_collect_day_snapshot" in globals():
    _GARMIN_COACH_ORIGINAL_COLLECT_DAY_SNAPSHOT = _collect_day_snapshot

    def _collect_day_snapshot(*args, **kwargs):
        snap = _GARMIN_COACH_ORIGINAL_COLLECT_DAY_SNAPSHOT(*args, **kwargs)
        if not isinstance(snap, dict):
            return snap

        metrics = snap.setdefault("metrics", {})
        raw = snap.get("raw_sources") or {}

        for key_en, key_es in (
            ("stress_label", "stress_label_es"),
            ("hrv_status", "hrv_status_es"),
            ("acute_load_status", "acute_load_status_es"),
            ("training_readiness_status", "training_readiness_status_es"),
        ):
            translated = _translate_metric_status_es(key_en, metrics.get(key_en))
            if translated:
                metrics[key_es] = translated

        translated_msg = _translate_training_readiness_message_es(
            metrics.get("training_readiness_message")
        )
        if translated_msg:
            metrics["training_readiness_message_es"] = translated_msg

        training_status = metrics.get("training_status") or _extract_training_status_code(
            raw.get("training_status_raw")
        )
        if training_status:
            metrics["training_status"] = training_status
            translated_training_status = _translate_training_status_es(training_status)
            if translated_training_status:
                metrics["training_status_es"] = translated_training_status

        return snap
# === GARMIN_ES_TRANSLATIONS_PATCH_END ===

# ==== ES_FINAL_TRANSLATIONS_PATCH_START ====

_ES_STATUS_MAP = {
    "BALANCED": "Equilibrado",
    "LOW": "Bajo",
    "MODERATE": "Moderada",
    "HIGH": "Alto",
    "OPTIMAL": "Óptimo",
    "PRODUCTIVE": "Productivo",
    "RECOVERY": "Recuperación",
    "STRAINED": "Sobrecarga",
    "OVERREACHING": "Exceso de carga",
    "DETRAINING": "Desentrenamiento",
    "MAINTAINING": "Mantenimiento",
    "PEAKING": "Pico de forma",
}

_ES_MESSAGE_MAP = {
    "WORKING_HARD": "Entrenando duro",
    "BALANCE_YOUR_TRAINING_LOAD": "Equilibra tu carga de entrenamiento",
}

ES_FIELD_LABELS = {
    "body_battery_current": "Batería corporal actual",
    "body_battery_max": "Batería corporal máxima",
    "body_battery_min": "Batería corporal mínima",
    "body_battery_charged": "Batería corporal cargada",
    "body_battery_drained": "Batería corporal drenada",
    "body_battery_status": "Estado de la batería corporal",
    "sleep_duration_seconds": "Duración del sueño",
    "sleep_hours": "Horas de sueño",
    "sleep_score": "Puntuación de sueño",
    "sleep_deep_min": "Sueño profundo",
    "sleep_rem_min": "Sueño REM",
    "sleep_light_min": "Sueño ligero",
    "sleep_awake_min": "Tiempo despierto",
    "resting_heart_rate": "FC en reposo",
    "resting_heart_rate_7d_avg": "FC en reposo media de 7 días",
    "stress_avg": "Estrés medio",
    "stress_max": "Estrés máximo",
    "stress_label": "Estado del estrés",
    "hrv_last_night": "VFC nocturna",
    "hrv_weekly_avg": "VFC media semanal",
    "hrv_status": "Estado de la VFC",
    "hrv_baseline_low": "Límite inferior equilibrado de la VFC",
    "hrv_baseline_high": "Límite superior equilibrado de la VFC",
    "hrv_last_night_5min_high": "Máximo nocturno de VFC en 5 min",
    "training_readiness_score": "Preparación para entrenar",
    "training_readiness_status": "Estado de preparación para entrenar",
    "training_readiness_message": "Mensaje de preparación para entrenar",
    "training_readiness_recovery_time": "Recuperación restante",
    "training_readiness_input_context": "Contexto de preparación para entrenar",
    "acute_load": "Carga aguda",
    "acute_load_ratio": "Ratio carga aguda/crónica",
    "acute_load_status": "Estado de la carga aguda",
    "steps": "Pasos",
    "steps_goal": "Objetivo de pasos",
    "vo2max": "VO2max",
}

ES_TERM_LABELS = {
    "hr": "FC",
    "rhr": "FC en reposo",
    "hrv": "VFC",
    "vo2max": "VO2max",
    "spo2": "SpO2",
    "rem": "REM",
    "body_battery": "Batería corporal",
}

def _translate_status_es(value):
    if value is None:
        return None
    return _ES_STATUS_MAP.get(str(value).strip().upper(), value)

def _translate_message_es(value):
    if value is None:
        return None
    return _ES_MESSAGE_MAP.get(str(value).strip().upper(), value)

try:
    _collect_day_snapshot_original_es_patch
except NameError:
    _collect_day_snapshot_original_es_patch = _collect_day_snapshot

def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_es_patch(*args, **kwargs)
    metrics = snap.get("metrics") or {}

    metrics["stress_label_es"] = _translate_status_es(metrics.get("stress_label"))
    metrics["hrv_status_es"] = _translate_status_es(metrics.get("hrv_status"))
    metrics["training_readiness_status_es"] = _translate_status_es(metrics.get("training_readiness_status"))
    metrics["training_readiness_message_es"] = _translate_message_es(metrics.get("training_readiness_message"))
    metrics["acute_load_status_es"] = _translate_status_es(metrics.get("acute_load_status"))

    snap["metrics"] = metrics
    return snap

# ==== ES_FINAL_TRANSLATIONS_PATCH_END ====

# === CANONICAL_ES_TRANSLATIONS_START ===
_FINAL_STATUS_ES = {
    "BALANCED": "Equilibrado",
    "UNBALANCED": "Desequilibrado",
    "LOW": "Bajo",
    "MODERATE": "Moderada",
    "HIGH": "Alto",
    "VERY_HIGH": "Muy alto",
    "OPTIMAL": "Óptimo",
    "PRODUCTIVE": "Productivo",
    "RECOVERY": "Recuperación",
    "UNPRODUCTIVE": "No productivo",
    "PEAK": "Pico",
    "MAINTAINING": "Mantenimiento",
    "OVERREACHING": "Exceso de carga",
}

_FINAL_MESSAGE_ES = {
    "WORKING_HARD": "Entrenando duro",
    "BALANCE_YOUR_TRAINING_LOAD": "Equilibra tu carga de entrenamiento",
    "UNKNOWN": "Desconocido",
    "PRODUCTIVE": "Productivo",
    "RECOVERY": "Recuperación",
    "UNPRODUCTIVE": "No productivo",
    "OVERREACHING": "Exceso de carga",
}

def _translate_status_es(value):
    if value is None:
        return None
    value = str(value).strip().upper()
    return _FINAL_STATUS_ES.get(value, value)

def _translate_message_es(value):
    if value is None:
        return None
    value = str(value).strip().upper()
    return _FINAL_MESSAGE_ES.get(value, value)

try:
    _collect_day_snapshot_original_es_final
except NameError:
    _collect_day_snapshot_original_es_final = _collect_day_snapshot

def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_es_final(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})

    metrics["stress_label_es"] = _translate_status_es(metrics.get("stress_label"))
    metrics["hrv_status_es"] = _translate_status_es(metrics.get("hrv_status"))
    metrics["training_readiness_status_es"] = _translate_status_es(metrics.get("training_readiness_status"))
    metrics["training_readiness_message_es"] = _translate_message_es(metrics.get("training_readiness_message"))
    metrics["acute_load_status_es"] = _translate_status_es(metrics.get("acute_load_status"))

    return snap
# === CANONICAL_ES_TRANSLATIONS_END ===


# === TRAINING READINESS RECOVERY GUARDRAILS START ===
_RECOVERY_STATE_ES = {
    "fresh": "Fresco",
    "estimated_from_last_activity": "Estimado desde la última actividad",
    "stale": "Desactualizado",
    "missing_timestamp": "Sin marca temporal",
    "missing": "Sin datos",
}


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_garmin_datetime(value: Any) -> datetime | None:
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            ts = float(value)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            dt = datetime.fromtimestamp(ts, tz=APP_TIMEZONE)
        except Exception:
            return None
    else:
        raw = str(value).strip()
        if not raw:
            return None

        candidates = [raw]
        if " " in raw and "T" not in raw:
            candidates.append(raw.replace(" ", "T", 1))

        dt = None
        for candidate in candidates:
            normalized = candidate
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(normalized)
                break
            except ValueError:
                continue

        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue

        if dt is None:
            return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=APP_TIMEZONE)
    return dt.astimezone(APP_TIMEZONE)


def _extract_latest_activity_end_local(raw_sources: Any) -> datetime | None:
    if not isinstance(raw_sources, dict):
        return None

    latest = None
    for key in ("recent_activities_raw", "activities_for_date_raw"):
        activities = raw_sources.get(key)
        if not isinstance(activities, list):
            continue

        for activity in activities:
            if not isinstance(activity, dict):
                continue

            start_dt = _parse_garmin_datetime(
                activity.get("endTimeLocal")
                or activity.get("stopTimeLocal")
                or activity.get("startTimeLocal")
                or activity.get("startTimeGMT")
                or activity.get("beginTimestamp")
            )
            if start_dt is None:
                continue

            end_dt = _parse_garmin_datetime(activity.get("endTimeLocal") or activity.get("stopTimeLocal"))
            if end_dt is None:
                duration_seconds = _safe_float(activity.get("duration"))
                if duration_seconds is not None:
                    end_dt = start_dt + timedelta(seconds=duration_seconds)
                else:
                    end_dt = start_dt

            if latest is None or end_dt > latest:
                latest = end_dt

    return latest


def _extract_recovery_value(entry: dict[str, Any]) -> tuple[float | None, str | None, str | None]:
    for key, unit in (
        ("recoveryMinutes", "minutes"),
        ("recoveryTimeMinutes", "minutes"),
        ("recoveryMin", "minutes"),
        ("recoveryHours", "hours"),
        ("recoveryTime", "hours_assumed"),
    ):
        value = _safe_float(entry.get(key))
        if value is not None:
            return value, unit, key
    return None, None, None


def _build_recovery_metrics(entry: Any, raw_sources: Any) -> dict[str, Any]:
    base_result: dict[str, Any] = {
        "training_readiness_recovery_time_raw": None,
        "training_readiness_recovery_time_raw_key": None,
        "training_readiness_recovery_time_unit": None,
        "training_readiness_recovery_time_unit_is_assumed": False,
        "training_readiness_recovery_reference_source": None,
        "training_readiness_recovery_reference_local": None,
        "training_readiness_recovery_age_minutes": None,
        "training_readiness_recovery_state": "missing",
        "training_readiness_recovery_state_es": _RECOVERY_STATE_ES.get("missing"),
        "training_readiness_recovery_is_stale": True,
        "training_readiness_recovery_minutes_remaining": None,
        "training_readiness_recovery_hours_remaining": None,
        "training_readiness_recovery_time": None,
        "training_readiness_recovery_safe_text": "Sin datos de recuperación",
        "training_readiness_recovery_answer_for_llm": "Sin datos de recuperación en este snapshot",
    }

    if not isinstance(entry, dict):
        return base_result

    raw_value, unit, raw_key = _extract_recovery_value(entry)
    reference_dt = _parse_garmin_datetime(entry.get("timestampLocal") or entry.get("timestamp"))
    reference_source = "training_readiness_timestamp"
    if reference_dt is None:
        reference_dt = _extract_latest_activity_end_local(raw_sources)
        if reference_dt is not None:
            reference_source = "last_activity_end"

    result: dict[str, Any] = {
        "training_readiness_recovery_time_raw": raw_value,
        "training_readiness_recovery_time_raw_key": raw_key,
        "training_readiness_recovery_time_unit": unit,
        "training_readiness_recovery_time_unit_is_assumed": unit == "hours_assumed",
        "training_readiness_recovery_reference_source": reference_source if reference_dt is not None else None,
        "training_readiness_recovery_reference_local": reference_dt.isoformat() if reference_dt is not None else None,
        "training_readiness_recovery_age_minutes": None,
        "training_readiness_recovery_state": "missing",
        "training_readiness_recovery_state_es": _RECOVERY_STATE_ES.get("missing"),
        "training_readiness_recovery_is_stale": True,
        "training_readiness_recovery_minutes_remaining": None,
        "training_readiness_recovery_hours_remaining": None,
        "training_readiness_recovery_time": None,
        "training_readiness_recovery_safe_text": "Sin datos de recuperación",
        "training_readiness_recovery_answer_for_llm": "Sin datos de recuperación en este snapshot",
    }

    if raw_value is None:
        state = "missing"
        age_minutes = None
    elif reference_dt is None:
        state = "missing_timestamp"
        age_minutes = None
    else:
        age_minutes = max(0, int((_now_local() - reference_dt).total_seconds() // 60))
        crossed_local_day = reference_dt.date() < _today_local()
        is_stale = age_minutes > RECOVERY_MAX_FRESH_MINUTES or (crossed_local_day and age_minutes > RECOVERY_CROSS_DAY_STALE_MINUTES)
        if is_stale:
            state = "stale"
        elif reference_source == "last_activity_end":
            state = "estimated_from_last_activity"
        else:
            state = "fresh"

    result["training_readiness_recovery_age_minutes"] = age_minutes
    result["training_readiness_recovery_state"] = state
    result["training_readiness_recovery_state_es"] = _RECOVERY_STATE_ES.get(state, state)
    result["training_readiness_recovery_is_stale"] = state in {"stale", "missing", "missing_timestamp"}

    if raw_value is None or state in {"stale", "missing", "missing_timestamp"} or unit is None:
        result.setdefault("training_readiness_recovery_minutes_remaining", None)
        result.setdefault("training_readiness_recovery_hours_remaining", None)
        result["training_readiness_recovery_time"] = 0 if raw_value == 0 else None

        if state == "stale":
            result["training_readiness_recovery_safe_text"] = "Dato de recuperación desactualizado; no extrapolar"
        elif state == "missing_timestamp":
            result["training_readiness_recovery_safe_text"] = "Sin marca temporal; no extrapolar"
        else:
            result["training_readiness_recovery_safe_text"] = "Sin datos de recuperación"

        result["training_readiness_recovery_answer_for_llm"] = result["training_readiness_recovery_safe_text"]
        return result

    if unit == "minutes":
        base_minutes = raw_value
    else:
        base_minutes = raw_value * 60.0

    remaining_minutes = max(0, int(round(base_minutes - float(age_minutes or 0))))
    remaining_hours = round(remaining_minutes / 60.0, 1)
    result["training_readiness_recovery_minutes_remaining"] = remaining_minutes
    result["training_readiness_recovery_hours_remaining"] = remaining_hours
    result["training_readiness_recovery_time"] = int((remaining_minutes + 59) // 60) if unit in {"hours", "hours_assumed"} else remaining_minutes

    if state == "fresh":
        if remaining_minutes == 0:
            result["training_readiness_recovery_safe_text"] = "0 min restantes"
        elif remaining_minutes < 60:
            result["training_readiness_recovery_safe_text"] = f"{remaining_minutes} min restantes"
        else:
            result["training_readiness_recovery_safe_text"] = f"{remaining_hours} h restantes"
    elif state == "estimated_from_last_activity":
        if remaining_minutes == 0:
            result["training_readiness_recovery_safe_text"] = "Estimación: 0 min restantes"
        elif remaining_minutes < 60:
            result["training_readiness_recovery_safe_text"] = f"Estimación: {remaining_minutes} min restantes"
        else:
            result["training_readiness_recovery_safe_text"] = f"Estimación: {remaining_hours} h restantes"
    elif state == "stale":
        result["training_readiness_recovery_safe_text"] = "Dato de recuperación desactualizado; no extrapolar"
    elif state == "missing_timestamp":
        result["training_readiness_recovery_safe_text"] = "Sin marca temporal; no extrapolar"
    else:
        result["training_readiness_recovery_safe_text"] = "Sin datos de recuperación"

    result["training_readiness_recovery_answer_for_llm"] = result["training_readiness_recovery_safe_text"]
    return result


try:
    _collect_day_snapshot_original_recovery_guardrails
except NameError:
    _collect_day_snapshot_original_recovery_guardrails = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_recovery_guardrails(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})
    raw_sources = snap.get("raw_sources") or {}

    entry = None
    selected_entry = metrics.get("training_readiness_selected_entry")
    if isinstance(selected_entry, dict):
        entry = selected_entry
    else:
        entry = _select_training_readiness_entry(raw_sources.get("training_readiness_raw"))

    recovery_metrics = _build_recovery_metrics(entry, raw_sources)
    metrics.update(recovery_metrics)

    selected_ts = None
    if isinstance(entry, dict):
        selected_ts = entry.get("timestampLocal") or entry.get("timestamp")
    metrics["training_readiness_selected_timestamp_local"] = selected_ts

    return snap


ES_FIELD_LABELS.update({
    "training_readiness_recovery_time_raw": "Recuperación Garmin bruta",
    "training_readiness_recovery_time_unit": "Unidad de recuperación Garmin",
    "training_readiness_recovery_reference_source": "Origen de la referencia de recuperación",
    "training_readiness_recovery_reference_local": "Referencia temporal de recuperación",
    "training_readiness_recovery_age_minutes": "Antigüedad de la recuperación (min)",
    "training_readiness_recovery_state": "Estado de frescura de la recuperación",
    "training_readiness_recovery_state_es": "Estado de frescura de la recuperación (ES)",
    "training_readiness_recovery_is_stale": "Recuperación desactualizada",
    "training_readiness_recovery_minutes_remaining": "Recuperación restante (min)",
    "training_readiness_recovery_hours_remaining": "Recuperación restante (h)",
    "training_readiness_recovery_safe_text": "Texto seguro de recuperación",
    "training_readiness_recovery_answer_for_llm": "Respuesta canónica de recuperación para LLM",
    "training_readiness_selected_timestamp_local": "Timestamp de la preparación para entrenar",
})
# === TRAINING READINESS RECOVERY GUARDRAILS END ===


# === CANONICAL UI/SPANISH FIELDS START ===
def _latest_known_data_timestamp_local(metrics: dict[str, Any]) -> str | None:
    candidates = []
    for key in (
        "body_battery_last_timestamp_local",
        "training_readiness_selected_timestamp_local",
        "training_readiness_recovery_reference_local",
    ):
        value = metrics.get(key)
        dt = _parse_garmin_datetime(value) if value is not None else None
        if dt is not None:
            candidates.append(dt)

    if not candidates:
        return None
    return max(candidates).isoformat()


try:
    _collect_day_snapshot_original_ui_canonical_fields
except NameError:
    _collect_day_snapshot_original_ui_canonical_fields = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_ui_canonical_fields(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})

    metrics["predisposicion_para_entrenar"] = metrics.get("training_readiness_score")
    readiness_status_es = _normalize_readiness_status_es(
        metrics.get("training_readiness_status_es")
        or metrics.get("training_readiness_status")
    )
    metrics["predisposicion_para_entrenar_estado"] = readiness_status_es
    metrics["predisposicion_para_entrenar_texto"] = (
        f'{metrics.get("training_readiness_score")} — {readiness_status_es}'
        if metrics.get("training_readiness_score") is not None and readiness_status_es
        else None
    )

    metrics["estado_vfc"] = metrics.get("hrv_status_es") or metrics.get("hrv_status")
    metrics["vfc_media_noche_ms"] = metrics.get("hrv_last_night")
    metrics["vfc_media_7_dias_ms"] = metrics.get("hrv_weekly_avg")

    readiness_entry = metrics.get("training_readiness_selected_entry") or {}
    if isinstance(readiness_entry, dict) and readiness_entry:
        metrics["predisposicion_factor_vfc_ms"] = readiness_entry.get("hrvWeeklyAverage")
        metrics["predisposicion_factor_sueno_score"] = readiness_entry.get("sleepScore")
        metrics["predisposicion_factor_recuperacion_raw"] = readiness_entry.get("recoveryTime")
        metrics["predisposicion_factor_carga_aguda"] = readiness_entry.get("acuteLoad")
        metrics["predisposicion_factor_feedback_vfc_raw"] = readiness_entry.get("hrvFactorFeedback")
        metrics["predisposicion_factor_feedback_recuperacion_raw"] = readiness_entry.get("recoveryTimeFactorFeedback")
        metrics["predisposicion_factor_feedback_sueno_reciente_raw"] = readiness_entry.get("sleepHistoryFactorFeedback")
        metrics["predisposicion_factor_feedback_estres_reciente_raw"] = readiness_entry.get("stressHistoryFactorFeedback")

    metrics["body_battery_actual"] = metrics.get("body_battery_current")
    metrics["body_battery_ultimo_timestamp_local"] = metrics.get("body_battery_last_timestamp_local")
    metrics["body_battery_texto"] = (
        f'{metrics.get("body_battery_current")} actual'
        if metrics.get("body_battery_current") is not None
        else None
    )

    metrics["puntuacion_de_sueno"] = metrics.get("sleep_score")
    metrics["duracion_de_sueno_texto"] = _format_duration_hm(metrics.get("sleep_duration_seconds"))
    metrics["sueno_texto_seguro"] = _build_sleep_safe_text(
        metrics.get("sleep_score"),
        metrics.get("duracion_de_sueno_texto"),
    )

    metrics["recuperacion_texto_seguro"] = (
        metrics.get("training_readiness_recovery_answer_for_llm")
        or metrics.get("training_readiness_recovery_safe_text")
    )

    metrics["snapshot_obtenido_local"] = _isoish_to_local(snap.get("fetched_at"))
    metrics["datos_hasta_local"] = _latest_known_data_timestamp_local(metrics)

    return snap


ES_FIELD_LABELS.update({
    "predisposicion_para_entrenar": "Predisposición para entrenar",
    "predisposicion_para_entrenar_estado": "Estado de predisposición para entrenar",
    "predisposicion_para_entrenar_texto": "Resumen de predisposición para entrenar",
    "estado_vfc": "Estado de VFC",
    "vfc_media_noche_ms": "VFC media nocturna (ms)",
    "vfc_media_7_dias_ms": "VFC media de 7 días (ms)",
    "body_battery_actual": "Body Battery actual",
    "body_battery_ultimo_timestamp_local": "Último timestamp de Body Battery",
    "body_battery_texto": "Resumen de Body Battery",
    "puntuacion_de_sueno": "Puntuación de sueño",
    "duracion_de_sueno_texto": "Duración de sueño",
    "sueno_texto_seguro": "Resumen de sueño",
    "recuperacion_texto_seguro": "Texto seguro de recuperación",
    "snapshot_obtenido_local": "Momento local de obtención del snapshot",
    "datos_hasta_local": "Datos disponibles hasta",
})
# === CANONICAL UI/SPANISH FIELDS END ===


# === HUMAN SLEEP PHASE FIELDS START ===
def _first_present_value_sleep(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _duration_text_from_metric_keys_sleep(metrics: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    value = _first_present_value_sleep(metrics, keys)
    if value is None:
        return None
    return _format_duration_hm(value)


try:
    _collect_day_snapshot_original_human_sleep_phase_fields
except NameError:
    _collect_day_snapshot_original_human_sleep_phase_fields = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_human_sleep_phase_fields(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})

    metrics["sueno_rem_texto"] = _duration_text_from_metric_keys_sleep(metrics, (
        "sleep_rem_seconds",
        "sleep_rem_duration_seconds",
        "sleep_rem_time_seconds",
        "rem_sleep_seconds",
        "remSleepSeconds",
        "remSleepDuration",
        "rem_seconds",
    ))

    metrics["sueno_profundo_texto"] = _duration_text_from_metric_keys_sleep(metrics, (
        "sleep_deep_seconds",
        "sleep_deep_duration_seconds",
        "deep_sleep_seconds",
        "deepSleepSeconds",
        "deepSleepDuration",
        "deep_seconds",
    ))

    metrics["sueno_ligero_texto"] = _duration_text_from_metric_keys_sleep(metrics, (
        "sleep_light_seconds",
        "sleep_light_duration_seconds",
        "light_sleep_seconds",
        "lightSleepSeconds",
        "lightSleepDuration",
        "light_seconds",
    ))

    metrics["sueno_despierto_texto"] = _duration_text_from_metric_keys_sleep(metrics, (
        "sleep_awake_seconds",
        "sleep_awake_duration_seconds",
        "awake_sleep_seconds",
        "awakeSleepSeconds",
        "awakeDuration",
        "sleep_wake_seconds",
        "awake_seconds",
    ))

    sueno_inicio_raw = _first_present_value_sleep(metrics, (
        "sleep_start_local",
        "sleep_start_time_local",
        "sleep_bedtime_local",
        "sleep_start_timestamp_local",
        "sleepStartTimestampLocal",
        "sleepTimeLocal",
        "sleep_start",
    ))
    sueno_fin_raw = _first_present_value_sleep(metrics, (
        "sleep_end_local",
        "sleep_end_time_local",
        "sleep_wake_time_local",
        "wake_time_local",
        "sleep_end_timestamp_local",
        "sleepEndTimestampLocal",
        "wakeTimeLocal",
        "sleep_end",
    ))

    metrics["sueno_inicio_texto"] = _short_local_dt_text(_isoish_to_local(sueno_inicio_raw))
    metrics["sueno_fin_texto"] = _short_local_dt_text(_isoish_to_local(sueno_fin_raw))

    fases = []
    if metrics.get("sueno_rem_texto"):
        fases.append(f'REM {metrics.get("sueno_rem_texto")}')
    if metrics.get("sueno_profundo_texto"):
        fases.append(f'Profundo {metrics.get("sueno_profundo_texto")}')
    if metrics.get("sueno_ligero_texto"):
        fases.append(f'Ligero {metrics.get("sueno_ligero_texto")}')
    if metrics.get("sueno_despierto_texto"):
        fases.append(f'Despierto {metrics.get("sueno_despierto_texto")}')

    metrics["sueno_fases_resumen_humano"] = ", ".join(fases) if fases else None

    return snap


ES_FIELD_LABELS.update({
    "sueno_rem_texto": "Sueño REM",
    "sueno_profundo_texto": "Sueño profundo",
    "sueno_ligero_texto": "Sueño ligero",
    "sueno_despierto_texto": "Tiempo despierto",
    "sueno_inicio_texto": "Inicio del sueño",
    "sueno_fin_texto": "Fin del sueño",
    "sueno_fases_resumen_humano": "Resumen de fases del sueño",
})
# === HUMAN SLEEP PHASE FIELDS START ===


# === RAW SLEEP DTO CANONICALIZATION START ===
def _parse_epoch_millis_to_local_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, tz=APP_TIMEZONE)
        return dt.isoformat()
    except Exception:
        return None


try:
    _collect_day_snapshot_original_raw_sleep_canonicalization
except NameError:
    _collect_day_snapshot_original_raw_sleep_canonicalization = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_raw_sleep_canonicalization(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})
    raw_sources = snap.get("raw_sources") or {}

    # Completar textos humanos que estaban quedando nulos
    if metrics.get("snapshot_obtenido_texto") is None:
        metrics["snapshot_obtenido_texto"] = _short_local_dt_text(metrics.get("snapshot_obtenido_local"))
    if metrics.get("datos_hasta_texto") is None:
        metrics["datos_hasta_texto"] = _short_local_dt_text(metrics.get("datos_hasta_local"))

    if metrics.get("body_battery_resumen_humano") is None:
        bb_actual = metrics.get("body_battery_actual")
        bb_nivel = metrics.get("body_battery_nivel_es")
        if bb_actual is not None and bb_nivel:
            metrics["body_battery_resumen_humano"] = f"{bb_actual} actual, nivel {bb_nivel}"
        elif bb_actual is not None:
            metrics["body_battery_resumen_humano"] = f"{bb_actual} actual"

    if metrics.get("estado_vfc_resumen_humano") is None:
        estado = metrics.get("estado_vfc")
        noche = metrics.get("vfc_media_noche_ms")
        media7 = metrics.get("vfc_media_7_dias_ms")
        if estado and noche is not None and media7 is not None:
            metrics["estado_vfc_resumen_humano"] = f"{estado}, {noche} ms nocturnos, {media7} ms de media 7 días"
        elif estado:
            metrics["estado_vfc_resumen_humano"] = str(estado)

    if metrics.get("sueno_resumen_humano") is None:
        safe = metrics.get("sueno_texto_seguro")
        if safe:
            metrics["sueno_resumen_humano"] = safe

    sleep_raw = raw_sources.get("sleep_raw") or {}
    daily = sleep_raw.get("dailySleepDTO") if isinstance(sleep_raw, dict) else None

    if isinstance(daily, dict):
        score = None
        sleep_scores = daily.get("sleepScores")
        if isinstance(sleep_scores, dict):
            overall = sleep_scores.get("overall")
            if isinstance(overall, dict):
                score = overall.get("value")
        if score is None:
            score = metrics.get("puntuacion_de_sueno")

        duration_seconds = daily.get("sleepTimeSeconds")
        if duration_seconds is None:
            duration_seconds = metrics.get("sleep_duration_seconds")

        rem_seconds = daily.get("remSleepSeconds")
        deep_seconds = daily.get("deepSleepSeconds")
        light_seconds = daily.get("lightSleepSeconds")
        awake_seconds = daily.get("awakeSleepSeconds")

        start_local_iso = (
            _parse_epoch_millis_to_local_iso(daily.get("sleepStartTimestampLocal"))
            or _parse_epoch_millis_to_local_iso(daily.get("sleepStartTimestampGMT"))
        )
        end_local_iso = (
            _parse_epoch_millis_to_local_iso(daily.get("sleepEndTimestampLocal"))
            or _parse_epoch_millis_to_local_iso(daily.get("sleepEndTimestampGMT"))
        )

        metrics["sueno_fecha_calendario"] = daily.get("calendarDate")
        metrics["sueno_origen_canonico"] = "raw_sources.sleep_raw.dailySleepDTO"
        metrics["sleep_score"] = score
        metrics["sleep_duration_seconds"] = duration_seconds

        metrics["puntuacion_de_sueno"] = score
        metrics["duracion_de_sueno_texto"] = _format_duration_hm(duration_seconds)
        metrics["sueno_texto_seguro"] = _build_sleep_safe_text(
            metrics.get("puntuacion_de_sueno"),
            metrics.get("duracion_de_sueno_texto"),
        )
        metrics["sueno_resumen_humano"] = metrics.get("sueno_texto_seguro")

        metrics["sueno_rem_texto"] = _format_duration_hm(rem_seconds)
        metrics["sueno_profundo_texto"] = _format_duration_hm(deep_seconds)
        metrics["sueno_ligero_texto"] = _format_duration_hm(light_seconds)
        metrics["sueno_despierto_texto"] = _format_duration_hm(awake_seconds)

        metrics["sueno_inicio_local"] = start_local_iso
        metrics["sueno_fin_local"] = end_local_iso
        metrics["sueno_inicio_texto"] = _short_local_dt_text(start_local_iso)
        metrics["sueno_fin_texto"] = _short_local_dt_text(end_local_iso)

        metrics["sueno_numero_despertares"] = daily.get("awakeCount")
        metrics["sueno_feedback_raw"] = daily.get("sleepScoreFeedback")
        metrics["sueno_insight_raw"] = daily.get("sleepScoreInsight")
        metrics["sueno_personalized_insight_raw"] = daily.get("sleepScorePersonalizedInsight")

        fases = []
        if metrics.get("sueno_rem_texto"):
            fases.append(f'REM {metrics.get("sueno_rem_texto")}')
        if metrics.get("sueno_profundo_texto"):
            fases.append(f'Profundo {metrics.get("sueno_profundo_texto")}')
        if metrics.get("sueno_ligero_texto"):
            fases.append(f'Ligero {metrics.get("sueno_ligero_texto")}')
        if metrics.get("sueno_despierto_texto"):
            fases.append(f'Despierto {metrics.get("sueno_despierto_texto")}')
        metrics["sueno_fases_resumen_humano"] = ", ".join(fases) if fases else None

        # Si el fin del sueño es más reciente que el "datos_hasta_local" previo, lo actualizamos
        current_datos_hasta = _parse_garmin_datetime(metrics.get("datos_hasta_local")) if metrics.get("datos_hasta_local") else None
        sleep_end_dt = _parse_garmin_datetime(end_local_iso) if end_local_iso else None
        if sleep_end_dt is not None and (current_datos_hasta is None or sleep_end_dt > current_datos_hasta):
            metrics["datos_hasta_local"] = sleep_end_dt.isoformat()
            metrics["datos_hasta_texto"] = _short_local_dt_text(metrics.get("datos_hasta_local"))

    return snap


ES_FIELD_LABELS.update({
    "sueno_fecha_calendario": "Fecha del sueño",
    "sueno_origen_canonico": "Origen canónico del sueño",
    "sueno_inicio_local": "Inicio local del sueño",
    "sueno_fin_local": "Fin local del sueño",
    "sueno_numero_despertares": "Número de despertares",
    "sueno_feedback_raw": "Feedback raw de sueño",
    "sueno_insight_raw": "Insight raw de sueño",
    "sueno_personalized_insight_raw": "Insight personalizado raw de sueño",
})
# === RAW SLEEP DTO CANONICALIZATION END ===


# === SLEEP GMT TIMESTAMP FIX START ===
def _epoch_millis_gmt_to_local_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=APP_TIMEZONE).isoformat()
    except Exception:
        return None


try:
    _collect_day_snapshot_original_sleep_gmt_timestamp_fix
except NameError:
    _collect_day_snapshot_original_sleep_gmt_timestamp_fix = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_sleep_gmt_timestamp_fix(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})
    raw_sources = snap.get("raw_sources") or {}

    sleep_raw = raw_sources.get("sleep_raw") or {}
    daily = sleep_raw.get("dailySleepDTO") if isinstance(sleep_raw, dict) else None

    if isinstance(daily, dict):
        start_from_gmt = _epoch_millis_gmt_to_local_iso(daily.get("sleepStartTimestampGMT"))
        end_from_gmt = _epoch_millis_gmt_to_local_iso(daily.get("sleepEndTimestampGMT"))

        if start_from_gmt:
            metrics["sueno_inicio_local"] = start_from_gmt
            metrics["sueno_inicio_texto"] = _short_local_dt_text(start_from_gmt)

        if end_from_gmt:
            metrics["sueno_fin_local"] = end_from_gmt
            metrics["sueno_fin_texto"] = _short_local_dt_text(end_from_gmt)

    return snap
# === SLEEP GMT TIMESTAMP FIX END ===


# === SLEEP FRESHNESS GUARDRAILS START ===
def _hours_between_local_datetimes(newer: Any, older: Any) -> float | None:
    newer_dt = _parse_garmin_datetime(newer) if newer is not None else None
    older_dt = _parse_garmin_datetime(older) if older is not None else None
    if newer_dt is None or older_dt is None:
        return None
    try:
        return round((newer_dt - older_dt).total_seconds() / 3600.0, 1)
    except Exception:
        return None


try:
    _collect_day_snapshot_original_sleep_freshness_guardrails
except NameError:
    _collect_day_snapshot_original_sleep_freshness_guardrails = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_sleep_freshness_guardrails(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})

    snapshot_local = metrics.get("snapshot_obtenido_local") or _now_local().isoformat()
    sleep_ref_local = metrics.get("sueno_fin_local") or metrics.get("sueno_referencia_local")
    sleep_ref_dt = _parse_garmin_datetime(sleep_ref_local) if sleep_ref_local is not None else None
    snapshot_dt = _parse_garmin_datetime(snapshot_local) if snapshot_local is not None else None

    state = "missing"
    if sleep_ref_dt is not None and snapshot_dt is not None:
        state = "fresh" if sleep_ref_dt.date() == snapshot_dt.date() else "stale"
    elif sleep_ref_dt is not None:
        state = "unknown"

    age_hours = _hours_between_local_datetimes(snapshot_local, sleep_ref_local)

    metrics["sueno_referencia_local"] = sleep_ref_dt.isoformat() if sleep_ref_dt is not None else None
    metrics["sueno_antiguedad_horas"] = age_hours
    metrics["sueno_estado_frescura"] = state
    metrics["sueno_es_actual"] = state == "fresh"

    summary = metrics.get("sueno_resumen_humano") or metrics.get("sueno_texto_seguro")
    phases = metrics.get("sueno_fases_resumen_humano")

    if state == "fresh":
        metrics["sueno_resumen_para_llm"] = summary
        metrics["sueno_fases_para_llm"] = phases
    elif state == "stale":
        ref_text = _short_local_dt_text(metrics.get("sueno_referencia_local")) or metrics.get("sueno_fecha_calendario")
        metrics["sueno_resumen_para_llm"] = f"Último sueño disponible del conector: {ref_text}; no asumir que corresponde a anoche"
        metrics["sueno_fases_para_llm"] = None
    elif state == "unknown":
        ref_text = _short_local_dt_text(metrics.get("sueno_referencia_local")) or "sin fecha clara"
        metrics["sueno_resumen_para_llm"] = f"Hay un sueño disponible ({ref_text}), pero no se pudo validar si corresponde a hoy"
        metrics["sueno_fases_para_llm"] = None
    else:
        metrics["sueno_resumen_para_llm"] = "No hay sueño usable en el snapshot actual"
        metrics["sueno_fases_para_llm"] = None

    return snap


@mcp.custom_route("/debug/sleep-freshness", methods=["GET"])
async def debug_sleep_freshness(_: Request) -> JSONResponse:
    with CACHE_LOCK:
        snapshot = deepcopy(CACHE.get("snapshot"))
        status = CACHE.get("status")
        last_refresh = CACHE.get("last_refresh")
        last_error = CACHE.get("last_error")

    metrics = {}
    if isinstance(snapshot, dict):
        metrics = snapshot.get("metrics") or {}

    keys = [
        "snapshot_obtenido_local",
        "snapshot_obtenido_texto",
        "sueno_fecha_calendario",
        "sueno_inicio_local",
        "sueno_fin_local",
        "sueno_inicio_texto",
        "sueno_fin_texto",
        "sueno_referencia_local",
        "sueno_antiguedad_horas",
        "sueno_estado_frescura",
        "sueno_es_actual",
        "puntuacion_de_sueno",
        "duracion_de_sueno_texto",
        "sueno_resumen_humano",
        "sueno_resumen_para_llm",
        "sueno_fases_resumen_humano",
        "sueno_fases_para_llm",
    ]

    payload = {
        "status": status,
        "last_refresh": last_refresh,
        "last_refresh_local": _isoish_to_local(last_refresh),
        "last_error": last_error,
        "snapshot_exists": isinstance(snapshot, dict),
        "metrics": {k: metrics.get(k) for k in keys},
    }
    return JSONResponse(payload)


ES_FIELD_LABELS.update({
    "sueno_referencia_local": "Referencia temporal del sueño",
    "sueno_antiguedad_horas": "Antigüedad del sueño (h)",
    "sueno_estado_frescura": "Estado de frescura del sueño",
    "sueno_es_actual": "Sueño actual",
    "sueno_resumen_para_llm": "Resumen seguro de sueño para LLM",
    "sueno_fases_para_llm": "Fases de sueño seguras para LLM",
})
# === SLEEP FRESHNESS GUARDRAILS END ===


# === MULTI_DAY_SLEEP_SELECTION START ===
def _find_sleep_client_in_args(*args, **kwargs):
    candidates = list(args) + list(kwargs.values())
    for obj in candidates:
        if hasattr(obj, "get_sleep_data") and callable(getattr(obj, "get_sleep_data")):
            return obj
    return None


def _sleep_candidate_from_raw(sleep_raw: Any) -> dict[str, Any] | None:
    if not isinstance(sleep_raw, dict):
        return None
    daily = sleep_raw.get("dailySleepDTO")
    if not isinstance(daily, dict):
        return None

    sleep_seconds = daily.get("sleepTimeSeconds")
    if sleep_seconds in (None, 0):
        return None

    end_local = (
        _epoch_millis_gmt_to_local_iso(daily.get("sleepEndTimestampGMT"))
        or _parse_epoch_millis_to_local_iso(daily.get("sleepEndTimestampLocal"))
    )
    start_local = (
        _epoch_millis_gmt_to_local_iso(daily.get("sleepStartTimestampGMT"))
        or _parse_epoch_millis_to_local_iso(daily.get("sleepStartTimestampLocal"))
    )

    end_dt = _parse_garmin_datetime(end_local) if end_local else None
    start_dt = _parse_garmin_datetime(start_local) if start_local else None

    if end_dt is None:
        return None

    return {
        "raw": sleep_raw,
        "daily": daily,
        "calendar_date": daily.get("calendarDate"),
        "sleep_seconds": sleep_seconds,
        "start_local": start_local,
        "end_local": end_local,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }


def _pick_latest_sleep_from_client(client: Any, snapshot_local_iso: str | None) -> dict[str, Any] | None:
    snapshot_dt = _parse_garmin_datetime(snapshot_local_iso) if snapshot_local_iso else _now_local()
    if snapshot_dt is None:
        snapshot_dt = _now_local()

    checked = []
    candidates = []

    for delta_days in (0, 1, 2):
        day = (snapshot_dt.date() - timedelta(days=delta_days)).isoformat()
        try:
            raw = client.get_sleep_data(day)
        except Exception as exc:
            checked.append({
                "requested_date": day,
                "ok": False,
                "error": str(exc),
            })
            continue

        candidate = _sleep_candidate_from_raw(raw)
        checked.append({
            "requested_date": day,
            "ok": candidate is not None,
            "calendar_date": candidate.get("calendar_date") if candidate else None,
            "end_local": candidate.get("end_local") if candidate else None,
            "sleep_seconds": candidate.get("sleep_seconds") if candidate else None,
        })

        if candidate is None:
            continue

        if candidate["end_dt"] <= snapshot_dt:
            candidates.append(candidate)

    if not candidates:
        return {
            "selected": None,
            "checked": checked,
        }

    selected = max(candidates, key=lambda c: c["end_dt"])
    return {
        "selected": selected,
        "checked": checked,
    }


def _apply_sleep_candidate_to_metrics(metrics: dict[str, Any], candidate: dict[str, Any], source_label: str) -> None:
    daily = candidate["daily"]

    score = None
    sleep_scores = daily.get("sleepScores")
    if isinstance(sleep_scores, dict):
        overall = sleep_scores.get("overall")
        if isinstance(overall, dict):
            score = overall.get("value")

    duration_seconds = daily.get("sleepTimeSeconds")
    rem_seconds = daily.get("remSleepSeconds")
    deep_seconds = daily.get("deepSleepSeconds")
    light_seconds = daily.get("lightSleepSeconds")
    awake_seconds = daily.get("awakeSleepSeconds")

    start_local_iso = candidate.get("start_local")
    end_local_iso = candidate.get("end_local")

    metrics["sueno_fecha_calendario"] = daily.get("calendarDate")
    metrics["sueno_origen_canonico"] = source_label
    metrics["sleep_score"] = score
    metrics["sleep_duration_seconds"] = duration_seconds

    metrics["puntuacion_de_sueno"] = score
    metrics["duracion_de_sueno_texto"] = _format_duration_hm(duration_seconds)
    metrics["sueno_texto_seguro"] = _build_sleep_safe_text(
        metrics.get("puntuacion_de_sueno"),
        metrics.get("duracion_de_sueno_texto"),
    )
    metrics["sueno_resumen_humano"] = metrics.get("sueno_texto_seguro")

    metrics["sueno_rem_texto"] = _format_duration_hm(rem_seconds)
    metrics["sueno_profundo_texto"] = _format_duration_hm(deep_seconds)
    metrics["sueno_ligero_texto"] = _format_duration_hm(light_seconds)
    metrics["sueno_despierto_texto"] = _format_duration_hm(awake_seconds)

    metrics["sueno_inicio_local"] = start_local_iso
    metrics["sueno_fin_local"] = end_local_iso
    metrics["sueno_inicio_texto"] = _short_local_dt_text(start_local_iso)
    metrics["sueno_fin_texto"] = _short_local_dt_text(end_local_iso)

    metrics["sueno_numero_despertares"] = daily.get("awakeCount")
    metrics["sueno_feedback_raw"] = daily.get("sleepScoreFeedback")
    metrics["sueno_insight_raw"] = daily.get("sleepScoreInsight")
    metrics["sueno_personalized_insight_raw"] = daily.get("sleepScorePersonalizedInsight")

    fases = []
    if metrics.get("sueno_rem_texto"):
        fases.append(f'REM {metrics.get("sueno_rem_texto")}')
    if metrics.get("sueno_profundo_texto"):
        fases.append(f'Profundo {metrics.get("sueno_profundo_texto")}')
    if metrics.get("sueno_ligero_texto"):
        fases.append(f'Ligero {metrics.get("sueno_ligero_texto")}')
    if metrics.get("sueno_despierto_texto"):
        fases.append(f'Despierto {metrics.get("sueno_despierto_texto")}')
    metrics["sueno_fases_resumen_humano"] = ", ".join(fases) if fases else None

    current_datos_hasta = _parse_garmin_datetime(metrics.get("datos_hasta_local")) if metrics.get("datos_hasta_local") else None
    sleep_end_dt = _parse_garmin_datetime(end_local_iso) if end_local_iso else None
    if sleep_end_dt is not None and (current_datos_hasta is None or sleep_end_dt > current_datos_hasta):
        metrics["datos_hasta_local"] = sleep_end_dt.isoformat()
        metrics["datos_hasta_texto"] = _short_local_dt_text(metrics.get("datos_hasta_local"))


def _recompute_sleep_freshness_fields(metrics: dict[str, Any]) -> None:
    snapshot_local = metrics.get("snapshot_obtenido_local") or _now_local().isoformat()
    sleep_ref_local = metrics.get("sueno_fin_local") or metrics.get("sueno_referencia_local")
    sleep_ref_dt = _parse_garmin_datetime(sleep_ref_local) if sleep_ref_local is not None else None
    snapshot_dt = _parse_garmin_datetime(snapshot_local) if snapshot_local is not None else None

    state = "missing"
    if sleep_ref_dt is not None and snapshot_dt is not None:
        state = "fresh" if sleep_ref_dt.date() == snapshot_dt.date() else "stale"
    elif sleep_ref_dt is not None:
        state = "unknown"

    age_hours = _hours_between_local_datetimes(snapshot_local, sleep_ref_local)

    metrics["sueno_referencia_local"] = sleep_ref_dt.isoformat() if sleep_ref_dt is not None else None
    metrics["sueno_antiguedad_horas"] = age_hours
    metrics["sueno_estado_frescura"] = state
    metrics["sueno_es_actual"] = state == "fresh"

    summary = metrics.get("sueno_resumen_humano") or metrics.get("sueno_texto_seguro")
    phases = metrics.get("sueno_fases_resumen_humano")

    if state == "fresh":
        metrics["sueno_resumen_para_llm"] = summary
        metrics["sueno_fases_para_llm"] = phases
    elif state == "stale":
        ref_text = _short_local_dt_text(metrics.get("sueno_referencia_local")) or metrics.get("sueno_fecha_calendario")
        metrics["sueno_resumen_para_llm"] = f"Último sueño disponible del conector: {ref_text}; no asumir que corresponde a anoche"
        metrics["sueno_fases_para_llm"] = None
    elif state == "unknown":
        ref_text = _short_local_dt_text(metrics.get("sueno_referencia_local")) or "sin fecha clara"
        metrics["sueno_resumen_para_llm"] = f"Hay un sueño disponible ({ref_text}), pero no se pudo validar si corresponde a hoy"
        metrics["sueno_fases_para_llm"] = None
    else:
        metrics["sueno_resumen_para_llm"] = "No hay sueño usable en el snapshot actual"
        metrics["sueno_fases_para_llm"] = None


try:
    _collect_day_snapshot_original_multi_day_sleep_selection
except NameError:
    _collect_day_snapshot_original_multi_day_sleep_selection = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_multi_day_sleep_selection(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})
    raw_sources = snap.setdefault("raw_sources", {})

    client = _find_sleep_client_in_args(*args, **kwargs)
    snapshot_local_iso = metrics.get("snapshot_obtenido_local") or _isoish_to_local(snap.get("fetched_at")) or _now_local().isoformat()

    selection = None
    if client is not None:
        selection = _pick_latest_sleep_from_client(client, snapshot_local_iso)

    if isinstance(selection, dict):
        raw_sources["sleep_selection_debug"] = selection.get("checked")

    selected = selection.get("selected") if isinstance(selection, dict) else None
    if selected is not None:
        raw_sources["sleep_raw"] = selected["raw"]
        source_label = f'garmin.get_sleep_data({selected["calendar_date"]})'
        _apply_sleep_candidate_to_metrics(metrics, selected, source_label)
        _recompute_sleep_freshness_fields(metrics)

    return snap


@mcp.custom_route("/debug/sleep-selection", methods=["GET"])
async def debug_sleep_selection(_: Request) -> JSONResponse:
    with CACHE_LOCK:
        snapshot = deepcopy(CACHE.get("snapshot"))
        status = CACHE.get("status")
        last_refresh = CACHE.get("last_refresh")
        last_error = CACHE.get("last_error")

    metrics = {}
    raw_sources = {}
    if isinstance(snapshot, dict):
        metrics = snapshot.get("metrics") or {}
        raw_sources = snapshot.get("raw_sources") or {}

    payload = {
        "status": status,
        "last_refresh": last_refresh,
        "last_refresh_local": _isoish_to_local(last_refresh),
        "last_error": last_error,
        "snapshot_exists": isinstance(snapshot, dict),
        "selected_sleep": {
            "sueno_fecha_calendario": metrics.get("sueno_fecha_calendario"),
            "sueno_origen_canonico": metrics.get("sueno_origen_canonico"),
            "sueno_inicio_texto": metrics.get("sueno_inicio_texto"),
            "sueno_fin_texto": metrics.get("sueno_fin_texto"),
            "sueno_estado_frescura": metrics.get("sueno_estado_frescura"),
            "sueno_es_actual": metrics.get("sueno_es_actual"),
            "sueno_antiguedad_horas": metrics.get("sueno_antiguedad_horas"),
            "sueno_resumen_para_llm": metrics.get("sueno_resumen_para_llm"),
            "sueno_fases_para_llm": metrics.get("sueno_fases_para_llm"),
        },
        "selection_debug": raw_sources.get("sleep_selection_debug"),
    }
    return JSONResponse(payload)
# === MULTI_DAY_SLEEP_SELECTION END ===


# === GARMIN GET_SLEEP_DATA MULTI-DAY WRAPPER START ===
_SLEEP_SELECTION_DEBUG_LAST = None


def _parse_iso_date_or_today(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if value is None:
        return _today_local()
    raw = str(value).strip()
    if not raw:
        return _today_local()
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return _today_local()


def _sleep_candidate_from_raw_for_wrapper(requested_date_iso: str, sleep_raw: Any) -> dict[str, Any] | None:
    if not isinstance(sleep_raw, dict):
        return None
    daily = sleep_raw.get("dailySleepDTO")
    if not isinstance(daily, dict):
        return None

    sleep_seconds = daily.get("sleepTimeSeconds")
    if sleep_seconds in (None, 0):
        return None

    end_local = (
        _epoch_millis_gmt_to_local_iso(daily.get("sleepEndTimestampGMT"))
        or _parse_epoch_millis_to_local_iso(daily.get("sleepEndTimestampLocal"))
    )
    start_local = (
        _epoch_millis_gmt_to_local_iso(daily.get("sleepStartTimestampGMT"))
        or _parse_epoch_millis_to_local_iso(daily.get("sleepStartTimestampLocal"))
    )

    end_dt = _parse_garmin_datetime(end_local) if end_local else None
    start_dt = _parse_garmin_datetime(start_local) if start_local else None
    if end_dt is None:
        return None

    return {
        "requested_date": requested_date_iso,
        "calendar_date": daily.get("calendarDate"),
        "sleep_seconds": sleep_seconds,
        "start_local": start_local,
        "end_local": end_local,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "raw": sleep_raw,
    }


try:
    _Garmin_get_sleep_data_original_multi_day
except NameError:
    try:
        _Garmin_get_sleep_data_original_multi_day = Garmin.get_sleep_data
    except Exception:
        _Garmin_get_sleep_data_original_multi_day = None


def _Garmin_get_sleep_data_multi_day(self, cdate):
    global _SLEEP_SELECTION_DEBUG_LAST

    if _Garmin_get_sleep_data_original_multi_day is None:
        raise RuntimeError("No se pudo capturar Garmin.get_sleep_data original")

    requested_date = _parse_iso_date_or_today(cdate)
    now_local = _now_local()

    checked = []
    candidates = []

    # Probamos el día pedido, ayer y anteayer
    offsets = (-1, 0, -2) if requested_date == _today_local() else (0, -1, -2)
    for offset in offsets:
        day = (requested_date + timedelta(days=offset)).isoformat()
        try:
            raw = _Garmin_get_sleep_data_original_multi_day(self, day)
        except Exception as exc:
            checked.append({
                "requested_date": day,
                "ok": False,
                "error": str(exc),
            })
            continue

        candidate = _sleep_candidate_from_raw_for_wrapper(day, raw)
        checked.append({
            "requested_date": day,
            "ok": candidate is not None,
            "calendar_date": candidate.get("calendar_date") if candidate else None,
            "end_local": candidate.get("end_local") if candidate else None,
            "sleep_seconds": candidate.get("sleep_seconds") if candidate else None,
        })

        if candidate is None:
            continue

        if candidate["end_dt"] <= now_local:
            candidates.append(candidate)

    selected = None
    if candidates:
        selected = max(candidates, key=lambda c: c["end_dt"])

    _SLEEP_SELECTION_DEBUG_LAST = {
        "requested_input": str(cdate),
        "requested_date_base": requested_date.isoformat(),
        "checked": checked,
        "selected": {
            "requested_date": selected.get("requested_date"),
            "calendar_date": selected.get("calendar_date"),
            "start_local": selected.get("start_local"),
            "end_local": selected.get("end_local"),
            "sleep_seconds": selected.get("sleep_seconds"),
        } if selected else None,
    }

    if selected is not None:
        return selected["raw"]

    return _Garmin_get_sleep_data_original_multi_day(self, requested_date.isoformat())


if _Garmin_get_sleep_data_original_multi_day is not None:
    try:
        Garmin.get_sleep_data = _Garmin_get_sleep_data_multi_day
    except Exception:
        pass


try:
    _collect_day_snapshot_original_sleep_selection_debug_bridge
except NameError:
    _collect_day_snapshot_original_sleep_selection_debug_bridge = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_sleep_selection_debug_bridge(*args, **kwargs)
    raw_sources = snap.setdefault("raw_sources", {})
    metrics = snap.setdefault("metrics", {})

    if _SLEEP_SELECTION_DEBUG_LAST is not None:
        raw_sources["sleep_selection_debug"] = deepcopy(_SLEEP_SELECTION_DEBUG_LAST)
        selected = _SLEEP_SELECTION_DEBUG_LAST.get("selected") or {}
        if selected:
            requested_date = selected.get("requested_date")
            calendar_date = selected.get("calendar_date")
            metrics["sueno_origen_canonico"] = f"garmin.get_sleep_data multi-day ({requested_date} -> {calendar_date})"

    return snap
# === GARMIN GET_SLEEP_DATA MULTI-DAY WRAPPER END ===



# === GARMIN GET_HRV_DATA MULTI-DAY WRAPPER START ===
_HRV_SELECTION_DEBUG_LAST = None


def _hrv_candidate_from_raw_for_wrapper(requested_date_iso: str, hrv_raw: Any) -> dict[str, Any] | None:
    if not isinstance(hrv_raw, dict):
        return None

    summary = hrv_raw.get("hrvSummary")
    if not isinstance(summary, dict):
        return None

    last_night = _pick_first_present(summary, ("lastNight", "lastNightAvg", "lastNightAverage"))
    weekly_avg = _pick_first_present(summary, ("weeklyAvg", "sevenDayAvg", "baselineAvg"))
    status = _pick_first_present(summary, ("hrvStatus", "status"))

    if last_night is None and weekly_avg is None and status is None:
        return None

    return {
        "requested_date": requested_date_iso,
        "last_night": last_night,
        "weekly_avg": weekly_avg,
        "status": status,
        "raw": hrv_raw,
    }


try:
    _Garmin_get_hrv_data_original_multi_day
except NameError:
    try:
        _Garmin_get_hrv_data_original_multi_day = Garmin.get_hrv_data
    except Exception:
        _Garmin_get_hrv_data_original_multi_day = None


def _Garmin_get_hrv_data_multi_day(self, cdate):
    global _HRV_SELECTION_DEBUG_LAST

    if _Garmin_get_hrv_data_original_multi_day is None:
        raise RuntimeError("No se pudo capturar Garmin.get_hrv_data original")

    requested_date = _parse_iso_date_or_today(cdate)
    checked = []
    selected = None

    for offset in (-1, 0, -2):
        day = (requested_date + timedelta(days=offset)).isoformat()
        try:
            raw = _Garmin_get_hrv_data_original_multi_day(self, day)
        except Exception as exc:
            checked.append({
                "requested_date": day,
                "ok": False,
                "error": str(exc),
            })
            continue

        candidate = _hrv_candidate_from_raw_for_wrapper(day, raw)
        checked.append({
            "requested_date": day,
            "ok": candidate is not None,
            "last_night": candidate.get("last_night") if candidate else None,
            "weekly_avg": candidate.get("weekly_avg") if candidate else None,
            "status": candidate.get("status") if candidate else None,
        })

        if candidate is not None:
            selected = candidate
            break

    _HRV_SELECTION_DEBUG_LAST = {
        "requested_input": str(cdate),
        "requested_date_base": requested_date.isoformat(),
        "checked": checked,
        "selected": {
            "requested_date": selected.get("requested_date"),
            "last_night": selected.get("last_night"),
            "weekly_avg": selected.get("weekly_avg"),
            "status": selected.get("status"),
        } if selected else None,
    }

    if selected is not None:
        return selected["raw"]

    return _Garmin_get_hrv_data_original_multi_day(self, requested_date.isoformat())


if _Garmin_get_hrv_data_original_multi_day is not None:
    try:
        Garmin.get_hrv_data = _Garmin_get_hrv_data_multi_day
    except Exception:
        pass


try:
    _collect_day_snapshot_original_hrv_selection_debug_bridge
except NameError:
    _collect_day_snapshot_original_hrv_selection_debug_bridge = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _collect_day_snapshot_original_hrv_selection_debug_bridge(*args, **kwargs)
    raw_sources = snap.setdefault("raw_sources", {})
    metrics = snap.setdefault("metrics", {})

    if _HRV_SELECTION_DEBUG_LAST is not None:
        raw_sources["hrv_selection_debug"] = deepcopy(_HRV_SELECTION_DEBUG_LAST)
        selected = _HRV_SELECTION_DEBUG_LAST.get("selected") or {}
        if selected:
            requested_date_base = _HRV_SELECTION_DEBUG_LAST.get("requested_date_base")
            source_date = selected.get("requested_date")
            metrics["vfc_fecha_api_garmin"] = source_date
            metrics["vfc_origen_canonico"] = f"garmin.get_hrv_data multi-day ({requested_date_base} -> {source_date})"
            try:
                intuitive_date = (date.fromisoformat(source_date) + timedelta(days=1)).isoformat()
            except Exception:
                intuitive_date = None
            metrics["vfc_noche_termina_en_fecha"] = intuitive_date

    if metrics.get("vfc_referencia_texto") is None:
        ref = metrics.get("vfc_noche_termina_en_fecha")
        if ref:
            try:
                ref_text = date.fromisoformat(ref).strftime("%d/%m/%Y")
            except Exception:
                ref_text = str(ref)
            fecha_api = metrics.get("vfc_fecha_api_garmin")
            if fecha_api and fecha_api != ref:
                metrics["vfc_referencia_texto"] = f"VFC nocturna de la noche que termina el {ref_text} (fecha API Garmin: {fecha_api})"
            else:
                metrics["vfc_referencia_texto"] = f"VFC nocturna de la noche que termina el {ref_text}"

    return snap


ES_FIELD_LABELS.update({
    "vfc_fecha_api_garmin": "Fecha API Garmin de VFC",
    "vfc_noche_termina_en_fecha": "Noche de VFC que termina en fecha",
    "vfc_origen_canonico": "Origen canónico de VFC",
    "vfc_referencia_texto": "Referencia humana de VFC",
})
# === GARMIN GET_HRV_DATA MULTI-DAY WRAPPER END ===


# === GARMIN PRESENTATION CLEANUP PATCH START ===
def _presentation_join(parts):
    return " · ".join([str(p) for p in parts if p not in (None, "", [], {})])


try:
    _GARMIN_PRESENTATION_CLEANUP_ORIGINAL_COLLECT_DAY_SNAPSHOT
except NameError:
    _GARMIN_PRESENTATION_CLEANUP_ORIGINAL_COLLECT_DAY_SNAPSHOT = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _GARMIN_PRESENTATION_CLEANUP_ORIGINAL_COLLECT_DAY_SNAPSHOT(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})
    raw = snap.setdefault("raw_sources", {})

    # Body Battery: texto humano más útil
    bb_actual = metrics.get("body_battery_actual")
    bb_max = metrics.get("body_battery_max")
    bb_min = metrics.get("body_battery_min")
    bb_charged = metrics.get("body_battery_charged")
    bb_drained = metrics.get("body_battery_drained")

    bb_parts = []
    if bb_actual is not None:
        bb_parts.append(f"Body Battery {bb_actual}")
    if bb_max is not None:
        bb_parts.append(f"máx {bb_max}")
    if bb_min is not None:
        bb_parts.append(f"mín {bb_min}")
    if bb_charged is not None:
        bb_parts.append(f"carga {bb_charged}")
    if bb_drained is not None:
        bb_parts.append(f"descarga {bb_drained}")

    bb_text = _presentation_join(bb_parts)
    if bb_text:
        metrics["body_battery_texto"] = bb_text
        metrics["body_battery_resumen_humano"] = bb_text

    # Predisposición: resumen humano de factores
    pred_parts = []
    pred_score = metrics.get("predisposicion_para_entrenar")
    pred_estado = metrics.get("predisposicion_para_entrenar_estado")
    pred_sueno = metrics.get("predisposicion_factor_sueno_score")
    pred_rec = metrics.get("predisposicion_factor_recuperacion_raw")
    pred_vfc = metrics.get("predisposicion_factor_vfc_ms")
    pred_carga = metrics.get("predisposicion_factor_carga_aguda")

    if pred_score is not None or pred_estado:
        head = _presentation_join([pred_score, pred_estado]).replace(" · ", " — ")
        if head:
            pred_parts.append(head)
    if pred_sueno is not None:
        pred_parts.append(f"sueño {pred_sueno}")
    if pred_rec is not None:
        pred_parts.append(f"recuperación raw {pred_rec}")
    if pred_vfc is not None:
        pred_parts.append(f"VFC factor {pred_vfc}")
    if pred_carga is not None:
        pred_parts.append(f"carga aguda {pred_carga}")

    pred_text = _presentation_join(pred_parts)
    if pred_text:
        metrics["predisposicion_factores_resumen_humano"] = pred_text

    # Peso: indicar fuente real
    body = raw.get("body_composition_raw") or {}
    user = ((raw.get("user_profile_raw") or {}).get("userData")) or {}
    total_average = body.get("totalAverage") or {}

    daily_weight = total_average.get("weight")
    profile_weight = user.get("weight")

    if daily_weight is not None:
        metrics["peso_referencia_texto"] = "Peso de composición corporal del día"
    elif profile_weight is not None:
        metrics["peso_referencia_texto"] = "Peso tomado del perfil de Garmin (sin medición corporal del día)"

    # Edad física: aclarar procedencia
    fitness_age_raw = raw.get("fitness_age_raw") or {}
    bmi_component = (fitness_age_raw.get("components") or {}).get("bmi") or {}
    bmi_last_measurement = bmi_component.get("lastMeasurementDate")

    if metrics.get("fitness_age") is not None:
        txt = "Edad física calculada por Garmin"
        if bmi_last_measurement:
            txt += f" · IMC con última medición {bmi_last_measurement}"
        metrics["fitness_age_referencia_texto"] = txt

    return snap


if "ES_FIELD_LABELS" in globals():
    ES_FIELD_LABELS.update({
        "predisposicion_factores_resumen_humano": "Resumen humano de factores de Predisposición",
        "peso_referencia_texto": "Referencia del peso",
        "fitness_age_referencia_texto": "Referencia de edad física",
    })
# === GARMIN PRESENTATION CLEANUP PATCH END ===


# === GARMIN ACCLIMATACION SPO2 PATCH START ===
def _first_non_none_local(*values):
    for v in values:
        if v is not None:
            return v
    return None


try:
    _GARMIN_ACCLIMATACION_SPO2_ORIGINAL_COLLECT_DAY_SNAPSHOT
except NameError:
    _GARMIN_ACCLIMATACION_SPO2_ORIGINAL_COLLECT_DAY_SNAPSHOT = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _GARMIN_ACCLIMATACION_SPO2_ORIGINAL_COLLECT_DAY_SNAPSHOT(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})
    raw = snap.setdefault("raw_sources", {})

    sleep_raw = raw.get("sleep_raw") or {}
    daily_sleep = sleep_raw.get("dailySleepDTO") or {}
    sleep_spo2_summary = sleep_raw.get("wellnessSpO2SleepSummaryDTO") or {}
    spo2_raw = raw.get("spo2_raw") or {}
    summary_raw = raw.get("summary_raw") or {}

    promedio_spo2 = _first_non_none_local(
        sleep_spo2_summary.get("averageSPO2"),
        daily_sleep.get("averageSpO2Value"),
        spo2_raw.get("avgSleepSpO2"),
    )
    spo2_minima = _first_non_none_local(
        sleep_spo2_summary.get("lowestSPO2"),
        daily_sleep.get("lowestSpO2Value"),
        spo2_raw.get("lowestSpO2"),
    )
    spo2_ultima = _first_non_none_local(
        spo2_raw.get("latestSpO2"),
        summary_raw.get("latestSpo2"),
    )
    spo2_media_general = _first_non_none_local(
        spo2_raw.get("averageSpO2"),
        summary_raw.get("averageSpo2"),
    )
    altitud_media = summary_raw.get("averageMonitoringEnvironmentAltitude")

    metrics["aclimatacion_spo2_promedio"] = promedio_spo2
    metrics["aclimatacion_spo2_minima"] = spo2_minima
    metrics["aclimatacion_spo2_ultima"] = spo2_ultima
    metrics["aclimatacion_spo2_media_general"] = spo2_media_general
    metrics["aclimatacion_altitud_media_entorno"] = altitud_media
    metrics["aclimatacion_spo2_hora_ultima_local"] = spo2_raw.get("latestSpO2TimestampLocal")
    metrics["aclimatacion_spo2_sueno_inicio_local"] = _first_non_none_local(
        spo2_raw.get("sleepStartTimestampLocal"),
        sleep_spo2_summary.get("sleepMeasurementStartGMT"),
    )
    metrics["aclimatacion_spo2_sueno_fin_local"] = _first_non_none_local(
        spo2_raw.get("sleepEndTimestampLocal"),
        sleep_spo2_summary.get("sleepMeasurementEndGMT"),
    )

    parts = []
    if promedio_spo2 is not None:
        parts.append(f"Promedio de SpO₂ {int(round(promedio_spo2))}%")
    if spo2_minima is not None:
        parts.append(f"mínimo {int(round(spo2_minima))}%")
    if spo2_ultima is not None:
        parts.append(f"última {int(round(spo2_ultima))}%")
    if altitud_media is not None:
        parts.append(f"altitud media {int(round(altitud_media))} m")

    resumen = " · ".join(parts)
    if resumen:
        metrics["aclimatacion_spo2_resumen_humano"] = resumen

    return snap


if "ES_FIELD_LABELS" in globals():
    ES_FIELD_LABELS.update({
        "aclimatacion_spo2_promedio": "Promedio de SpO₂ de aclimatación",
        "aclimatacion_spo2_minima": "SpO₂ mínima de aclimatación",
        "aclimatacion_spo2_ultima": "Última SpO₂ de aclimatación",
        "aclimatacion_spo2_media_general": "SpO₂ media general",
        "aclimatacion_altitud_media_entorno": "Altitud media del entorno",
        "aclimatacion_spo2_hora_ultima_local": "Hora local de la última SpO₂",
        "aclimatacion_spo2_sueno_inicio_local": "Inicio local de sueño para SpO₂",
        "aclimatacion_spo2_sueno_fin_local": "Fin local de sueño para SpO₂",
        "aclimatacion_spo2_resumen_humano": "Resumen humano de aclimatación por pulsioximetría",
    })
# === GARMIN ACCLIMATACION SPO2 PATCH END ===


# === GARMIN LACTATO PARCIAL PATCH START ===
try:
    _GARMIN_LACTATO_PARCIAL_ORIGINAL_COLLECT_DAY_SNAPSHOT
except NameError:
    _GARMIN_LACTATO_PARCIAL_ORIGINAL_COLLECT_DAY_SNAPSHOT = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _GARMIN_LACTATO_PARCIAL_ORIGINAL_COLLECT_DAY_SNAPSHOT(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})
    raw = snap.setdefault("raw_sources", {})

    user = ((raw.get("user_profile_raw") or {}).get("userData")) or {}

    lact_hr = user.get("lactateThresholdHeartRate")
    lact_speed = user.get("lactateThresholdSpeed")
    lact_auto = user.get("thresholdHeartRateAutoDetected")

    metrics["umbral_lactato_fc_ppm"] = lact_hr
    metrics["umbral_lactato_autodetectado"] = lact_auto
    metrics["umbral_lactato_ritmo_disponible"] = False
    metrics["umbral_lactato_potencia_disponible"] = False
    metrics["umbral_lactato_wkg_disponible"] = False
    metrics["umbral_lactato_speed_raw"] = lact_speed

    parts = []
    if lact_hr is not None:
        parts.append(f"{int(round(lact_hr))} ppm")
    if lact_auto is True:
        parts.append("autodetectado")
    elif lact_auto is False:
        parts.append("no autodetectado")
    parts.append("ritmo/potencia/W/kg no disponibles con las fuentes actuales")

    metrics["umbral_lactato_resumen_humano"] = " · ".join(parts)

    return snap


if "ES_FIELD_LABELS" in globals():
    ES_FIELD_LABELS.update({
        "umbral_lactato_fc_ppm": "Umbral de lactato (frecuencia cardiaca)",
        "umbral_lactato_autodetectado": "Umbral de lactato autodetectado",
        "umbral_lactato_ritmo_disponible": "Ritmo de umbral disponible",
        "umbral_lactato_potencia_disponible": "Potencia de umbral disponible",
        "umbral_lactato_wkg_disponible": "Potencia relativa de umbral disponible",
        "umbral_lactato_speed_raw": "Velocidad bruta de umbral de lactato",
        "umbral_lactato_resumen_humano": "Resumen humano de umbral de lactato",
    })
# === GARMIN LACTATO PARCIAL PATCH END ===


# === GARMIN UI TEXTS PATCH START ===
def _gfmt_int(v):
    try:
        return f"{int(round(float(v))):,}".replace(",", ".")
    except Exception:
        return None

def _gfmt_km(v):
    try:
        return f"{float(v):.1f}".replace(".", ",") + " km"
    except Exception:
        return None

def _gsec_to_text(seconds):
    try:
        seconds = int(seconds)
    except Exception:
        return None
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}min"
    if h:
        return f"{h}h"
    return f"{m}min"


try:
    _GARMIN_UI_TEXTS_ORIGINAL_COLLECT_DAY_SNAPSHOT
except NameError:
    _GARMIN_UI_TEXTS_ORIGINAL_COLLECT_DAY_SNAPSHOT = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _GARMIN_UI_TEXTS_ORIGINAL_COLLECT_DAY_SNAPSHOT(*args, **kwargs)
    metrics = snap.setdefault("metrics", {})
    raw = snap.setdefault("raw_sources", {})

    summary = raw.get("summary_raw") or {}
    load_balance_map = (((raw.get("training_status_raw") or {}).get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap")) or {}
    load_balance = None
    if isinstance(load_balance_map, dict):
        for block in load_balance_map.values():
            if isinstance(block, dict):
                load_balance = block
                break
    load_balance = load_balance or {}

    # Calorías
    active_kcal = metrics.get("active_kcal")
    total_kcal = metrics.get("total_kcal")
    rest_kcal = summary.get("bmrKilocalories")
    if rest_kcal is None and active_kcal is not None and total_kcal is not None:
        rest_kcal = float(total_kcal) - float(active_kcal)

    metrics["calorias_activas"] = active_kcal
    metrics["calorias_en_reposo"] = rest_kcal
    metrics["calorias_totales"] = total_kcal

    if active_kcal is not None and rest_kcal is not None and total_kcal is not None:
        metrics["calorias_resumen_humano"] = (
            f"{_gfmt_int(active_kcal)} Calorías activas + "
            f"{_gfmt_int(rest_kcal)} Calorías en reposo = "
            f"{_gfmt_int(total_kcal)} Total de calorías quemadas"
        )

    # Pasos
    steps = metrics.get("steps")
    steps_goal = metrics.get("steps_goal")
    distance_km = metrics.get("distance_km")
    pasos_parts = []
    if steps is not None:
        pasos_parts.append(f"{_gfmt_int(steps)} pasos")
    if steps_goal is not None:
        pasos_parts.append(f"objetivo {_gfmt_int(steps_goal)}")
    if distance_km is not None:
        pasos_parts.append(f"distancia {_gfmt_km(distance_km)}")
    if pasos_parts:
        metrics["pasos_resumen_humano"] = " · ".join(pasos_parts)

    # Pisos
    floors_up = summary.get("floorsAscended")
    floors_down = summary.get("floorsDescended")
    floors_goal = summary.get("userFloorsAscendedGoal")
    metrics["pisos_subidos"] = floors_up
    metrics["pisos_bajados"] = floors_down
    metrics["pisos_objetivo"] = floors_goal
    pisos_parts = []
    if floors_up is not None:
        pisos_parts.append(f"{_gfmt_int(floors_up)} subidos")
    if floors_down is not None:
        pisos_parts.append(f"{_gfmt_int(floors_down)} bajados")
    if floors_goal is not None:
        pisos_parts.append(f"objetivo {_gfmt_int(floors_goal)}")
    if pisos_parts:
        metrics["pisos_resumen_humano"] = " · ".join(pisos_parts)

    # Minutos de intensidad
    intensity = raw.get("intensity_minutes_raw") or {}
    weekly_total = intensity.get("weeklyTotal")
    weekly_mod = intensity.get("weeklyModerate")
    weekly_vig = intensity.get("weeklyVigorous")
    week_goal = intensity.get("weekGoal") or summary.get("intensityMinutesGoal")

    metrics["minutos_intensidad_total_semanal"] = weekly_total
    metrics["minutos_intensidad_moderados_semanal"] = weekly_mod
    metrics["minutos_intensidad_altos_semanal"] = weekly_vig
    metrics["minutos_intensidad_objetivo_semanal"] = week_goal

    im_parts = []
    if weekly_total is not None:
        im_parts.append(f"{_gfmt_int(weekly_total)} minutos de intensidad")
    if weekly_mod is not None:
        im_parts.append(f"{_gfmt_int(weekly_mod)} moderados")
    if weekly_vig is not None:
        im_parts.append(f"{_gfmt_int(weekly_vig)} altos")
    if week_goal is not None:
        im_parts.append(f"objetivo semanal {_gfmt_int(week_goal)}")
    if im_parts:
        metrics["minutos_intensidad_resumen_humano"] = " · ".join(im_parts)

    # Estrés
    stress_avg = metrics.get("stress_avg")
    rest_dur = _gsec_to_text(summary.get("restStressDuration"))
    low_dur = _gsec_to_text(summary.get("lowStressDuration"))
    med_dur = _gsec_to_text(summary.get("mediumStressDuration"))
    high_dur = _gsec_to_text(summary.get("highStressDuration"))

    estres_parts = []
    if stress_avg is not None:
        estres_parts.append(f"Nivel de estrés {_gfmt_int(stress_avg)}")
    if rest_dur:
        estres_parts.append(f"Descanso {rest_dur}")
    if low_dur:
        estres_parts.append(f"Bajo {low_dur}")
    if med_dur:
        estres_parts.append(f"Medio {med_dur}")
    if high_dur:
        estres_parts.append(f"Alta {high_dur}")
    if estres_parts:
        metrics["estres_resumen_humano"] = " · ".join(estres_parts)

    # Foco de carga
    foco = None
    al = load_balance.get("monthlyLoadAerobicLow")
    ah = load_balance.get("monthlyLoadAerobicHigh")
    an = load_balance.get("monthlyLoadAnaerobic")
    al_max = load_balance.get("monthlyLoadAerobicLowTargetMax")
    ah_max = load_balance.get("monthlyLoadAerobicHighTargetMax")
    an_max = load_balance.get("monthlyLoadAnaerobicTargetMax")

    try:
        if None not in (al, ah, an, al_max, ah_max, an_max):
            if al > al_max and ah > ah_max and an > an_max:
                foco = "Por encima de los objetivos"
    except Exception:
        pass

    if foco:
        metrics["foco_de_carga_texto"] = foco

    # Estado de entreno: resumen más Garmin
    training_status_es = metrics.get("training_status_es")
    vo2 = metrics.get("vo2max")
    vo2_label = metrics.get("vo2max_label")
    vfc_factor = metrics.get("predisposicion_factor_vfc_ms")
    acute = metrics.get("acute_load")
    acute_es = metrics.get("acute_load_status_es")

    et_parts = []
    if training_status_es:
        et_parts.append(training_status_es)
    if vo2 is not None:
        vo2_txt = f"VO2 máximo {int(round(float(vo2)))}"
        if vo2_label:
            vo2_txt += f" ({vo2_label})"
        et_parts.append(vo2_txt)
    if vfc_factor is not None:
        estado_vfc = metrics.get("estado_vfc")
        vfc_txt = f"Estado de VFC {int(round(float(vfc_factor)))} ms"
        if estado_vfc:
            vfc_txt += f" ({estado_vfc})"
        et_parts.append(vfc_txt)
    if acute is not None:
        acute_txt = f"Carga aguda {int(round(float(acute)))}"
        if acute_es:
            acute_txt += f" ({acute_es})"
        et_parts.append(acute_txt)
    if foco:
        et_parts.append(f"Foco de carga {foco}")
    if et_parts:
        metrics["estado_entreno_resumen_humano"] = " · ".join(et_parts)

    # Labels visibles
    if "ES_FIELD_LABELS" in globals():
        ES_FIELD_LABELS.update({
            "calorias_activas": "Calorías activas",
            "calorias_en_reposo": "Calorías en reposo",
            "calorias_totales": "Total de calorías quemadas",
            "calorias_resumen_humano": "Resumen humano de calorías",
            "pasos_resumen_humano": "Resumen humano de pasos",
            "pisos_subidos": "Subidos",
            "pisos_bajados": "Bajados",
            "pisos_objetivo": "Objetivo de pisos",
            "pisos_resumen_humano": "Resumen humano de pisos",
            "minutos_intensidad_total_semanal": "Minutos de intensidad semanales",
            "minutos_intensidad_moderados_semanal": "Minutos moderados semanales",
            "minutos_intensidad_altos_semanal": "Minutos altos semanales",
            "minutos_intensidad_objetivo_semanal": "Objetivo semanal de minutos de intensidad",
            "minutos_intensidad_resumen_humano": "Resumen humano de minutos de intensidad",
            "estres_resumen_humano": "Resumen humano de estrés",
            "foco_de_carga_texto": "Foco de carga",
            "estado_entreno_resumen_humano": "Resumen humano de estado de entreno",
        })

    return snap
# === GARMIN UI TEXTS PATCH END ===


# === MCPX ACTIVITY DEEP PATCH START ===
_ACTIVITY_SUMMARY_KEYS = [
    "distance",
    "duration",
    "elapsedDuration",
    "movingDuration",
    "calories",
    "activityTrainingLoad",
    "trainingEffect",
    "anaerobicTrainingEffect",
    "trainingEffectLabel",
    "aerobicTrainingEffectMessage",
    "anaerobicTrainingEffectMessage",
    "averageHR",
    "maxHR",
    "minHR",
    "averageSpeed",
    "averageMovingSpeed",
    "maxSpeed",
    "avgGradeAdjustedSpeed",
    "averagePower",
    "maxPower",
    "minPower",
    "normalizedPower",
    "totalWork",
    "averageRunCadence",
    "maxRunCadence",
    "groundContactTime",
    "verticalOscillation",
    "verticalRatio",
    "strideLength",
    "steps",
    "averageTemperature",
    "maxTemperature",
    "minTemperature",
    "avgElevation",
    "maxElevation",
    "minElevation",
    "elevationGain",
    "elevationLoss",
    "beginPotentialStamina",
    "endPotentialStamina",
    "minAvailableStamina",
    "differenceBodyBattery",
    "waterEstimated",
    "moderateIntensityMinutes",
    "vigorousIntensityMinutes",
]

_ACTIVITY_TRANSPORT_TYPES = {"motorcycling", "driving", "car", "automotive"}
_ACTIVITY_ENDURANCE_TYPES = {"running", "treadmill_running", "walking", "hiking", "trail_running", "track_running"}
_ACTIVITY_STRENGTH_TYPES = {"strength_training"}
_ACTIVITY_CYCLING_TYPES = {"cycling", "indoor_cycling", "mountain_biking", "road_biking", "virtual_ride"}
_ACTIVITY_SWIM_TYPES = {"lap_swimming", "open_water_swimming", "swimming"}


def _activity_type_key_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("activityTypeDTO", "activityType"):
        value = payload.get(key)
        if isinstance(value, dict) and value.get("typeKey"):
            return str(value.get("typeKey"))
    return None


def _activity_family(activity_type: str | None) -> str:
    if not activity_type:
        return "other"
    if activity_type in _ACTIVITY_ENDURANCE_TYPES:
        return "endurance"
    if activity_type in _ACTIVITY_STRENGTH_TYPES:
        return "strength"
    if activity_type in _ACTIVITY_CYCLING_TYPES:
        return "cycling"
    if activity_type in _ACTIVITY_SWIM_TYPES:
        return "swimming"
    if activity_type in _ACTIVITY_TRANSPORT_TYPES:
        return "transport"
    return activity_type


def _pick_activity_summary(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    return {key: summary.get(key) for key in _ACTIVITY_SUMMARY_KEYS if summary.get(key) is not None}


def _pick_activity_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    keys = [
        "lapCount",
        "hasChartData",
        "hasPolyline",
        "hasHrTimeInZones",
        "hasPowerTimeInZones",
        "hasSplits",
        "hasIntensityIntervals",
        "manufacturer",
        "fileFormat",
        "elevationCorrected",
        "trimmed",
        "personalRecord",
        "favorite",
        "associatedWorkoutId",
        "videoUrl",
    ]
    return {key: metadata.get(key) for key in keys if metadata.get(key) is not None}


def _extract_metric_descriptors(details: Any) -> list[dict[str, Any]]:
    if not isinstance(details, dict):
        return []
    out: list[dict[str, Any]] = []
    for descriptor in details.get("metricDescriptors") or []:
        if not isinstance(descriptor, dict):
            continue
        unit = descriptor.get("unit") or {}
        out.append({
            "metrics_index": descriptor.get("metricsIndex"),
            "key": descriptor.get("key"),
            "unit": unit.get("key") if isinstance(unit, dict) else None,
        })
    return out


def _extract_detail_counts(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {}
    polyline = None
    geo = details.get("geoPolylineDTO")
    if isinstance(geo, dict):
        polyline = geo.get("polyline")
    return {
        "details_available": details.get("detailsAvailable"),
        "measurement_count": details.get("measurementCount"),
        "metrics_count": details.get("metricsCount"),
        "total_metrics_count": details.get("totalMetricsCount"),
        "heart_rate_samples": len(details.get("heartRateDTOs") or []) if isinstance(details.get("heartRateDTOs"), list) else 0,
        "polyline_points": len(polyline) if isinstance(polyline, list) else 0,
    }


def _extract_metric_values_from_row(row: Any) -> list[Any]:
    if isinstance(row, dict):
        metrics = row.get("metrics")
        return metrics if isinstance(metrics, list) else []
    return row if isinstance(row, list) else []


def _compact_activity_time_series(details: Any, max_samples: int = 200) -> dict[str, Any]:
    if not isinstance(details, dict):
        return {
            "metric_descriptors": [],
            "sample_count": 0,
            "samples_returned": 0,
            "samples": [],
        }

    max_samples = max(1, min(2000, int(max_samples)))
    descriptors = _extract_metric_descriptors(details)
    rows = details.get("activityDetailMetrics") or []
    samples: list[dict[str, Any]] = []

    for row in rows[:max_samples]:
        values = _extract_metric_values_from_row(row)
        mapped: dict[str, Any] = {}
        for descriptor in descriptors:
            raw_index = descriptor.get("metrics_index")
            try:
                index = int(raw_index)
            except Exception:
                continue
            if index < 0 or index >= len(values):
                continue
            value = values[index]
            if value is None:
                continue
            key = descriptor.get("key") or f"metric_{index}"
            mapped[key] = value
        if mapped:
            samples.append(mapped)

    return {
        "metric_descriptors": descriptors,
        "sample_count": len(rows) if isinstance(rows, list) else 0,
        "samples_returned": len(samples),
        "samples": samples,
    }


def _extract_split_counts(splits: Any, typed_splits: Any, split_summaries: Any) -> dict[str, Any]:
    lap_count = 0
    typed_count = 0
    split_summary_count = 0

    if isinstance(splits, dict) and isinstance(splits.get("lapDTOs"), list):
        lap_count = len(splits.get("lapDTOs") or [])
    if isinstance(typed_splits, dict) and isinstance(typed_splits.get("splits"), list):
        typed_count = len(typed_splits.get("splits") or [])
    if isinstance(split_summaries, dict) and isinstance(split_summaries.get("splitSummaries"), list):
        split_summary_count = len(split_summaries.get("splitSummaries") or [])

    return {
        "laps": lap_count,
        "typed_splits": typed_count,
        "split_summaries": split_summary_count,
    }


def _exercise_set_count(exercise_sets: Any) -> int:
    if isinstance(exercise_sets, dict) and isinstance(exercise_sets.get("exerciseSets"), list):
        return len(exercise_sets.get("exerciseSets") or [])
    return 0


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _normalize_strength_weight_to_kg(value: Any) -> float | None:
    raw = _to_float_or_none(value)
    if raw is None:
        return None
    # En Garmin fuerza suele venir en gramos: 45000 -> 45.0 kg
    if raw >= 1000:
        return raw / 1000.0
    return raw


def _best_exercise_guess(item: dict[str, Any]) -> tuple[str, float | None]:
    exercises = item.get("exercises") or []
    if not isinstance(exercises, list) or not exercises:
        return ("UNKNOWN", None)

    best = None
    best_prob = None
    for ex in exercises:
        if not isinstance(ex, dict):
            continue
        cat = ex.get("category") or ex.get("name") or "UNKNOWN"
        prob = _to_float_or_none(ex.get("probability"))
        if best is None:
            best = cat
            best_prob = prob
            continue
        if prob is not None and (best_prob is None or prob > best_prob):
            best = cat
            best_prob = prob

    return (best or "UNKNOWN", best_prob)


def _summarize_strength_sets(exercise_sets_payload: Any) -> dict[str, Any] | None:
    if not isinstance(exercise_sets_payload, dict):
        return None

    exercise_sets = exercise_sets_payload.get("exerciseSets") or []
    total_sets_raw = exercise_sets_payload.get("totalSets")
    active_sets_raw = exercise_sets_payload.get("activeSets")

    per_set = []
    grouped: dict[str, dict[str, Any]] = {}

    total_reps = 0.0
    total_volume_kg = 0.0
    max_weight_kg = None
    active_sets_count = 0
    active_time_s = 0.0
    rest_time_s = 0.0

    if isinstance(exercise_sets, list):
        for idx, item in enumerate(exercise_sets):
            if not isinstance(item, dict):
                continue

            reps = _to_float_or_none(item.get("repetitionCount")) or 0.0
            weight_kg = _normalize_strength_weight_to_kg(item.get("weight")) or 0.0
            duration_s = _to_float_or_none(item.get("duration"))
            set_type = item.get("setType")
            start_time = item.get("startTime")
            guess, prob = _best_exercise_guess(item)
            volume_kg = reps * weight_kg

            if set_type == "ACTIVE":
                active_sets_count += 1
                if duration_s is not None:
                    active_time_s += duration_s
            elif set_type == "REST":
                if duration_s is not None:
                    rest_time_s += duration_s

            total_reps += reps
            total_volume_kg += volume_kg
            if max_weight_kg is None or weight_kg > max_weight_kg:
                max_weight_kg = weight_kg

            per_set.append({
                "set_index": idx,
                "exercise_guess": guess,
                "exercise_guess_probability": prob,
                "set_type": set_type,
                "start_time": start_time,
                "duration_s": duration_s,
                "reps": reps,
                "weight_kg": weight_kg,
                "volume_kg": volume_kg,
                "exercise_candidates": item.get("exercises"),
            })

            if set_type == "ACTIVE":
                row = grouped.setdefault(guess, {
                    "exercise": guess,
                    "sets": 0,
                    "active_sets": 0,
                    "reps": 0.0,
                    "max_weight_kg": None,
                    "volume_kg": 0.0,
                })
                row["sets"] += 1
                row["active_sets"] += 1
                row["reps"] += reps
                row["volume_kg"] += volume_kg
                if row["max_weight_kg"] is None or weight_kg > row["max_weight_kg"]:
                    row["max_weight_kg"] = weight_kg

    top_exercises = sorted(
        grouped.values(),
        key=lambda x: (x.get("volume_kg") or 0.0, x.get("sets") or 0),
        reverse=True,
    )

    return {
        "exercise_set_count": _exercise_set_count(exercise_sets_payload),
        "total_sets_raw": total_sets_raw,
        "active_sets_raw": active_sets_raw,
        "active_sets_count_estimated": active_sets_count,
        "active_time_s_estimated": active_time_s,
        "rest_time_s_estimated": rest_time_s,
        "total_reps_estimated": total_reps,
        "total_volume_kg_estimated": total_volume_kg,
        "max_weight_kg_seen": max_weight_kg,
        "top_exercises": top_exercises[:15],
        "per_set": per_set,
    }


def _sleep_with_jitter(base_seconds: float) -> None:
    import random
    import time
    time.sleep(base_seconds + random.uniform(0, 0.35))


def _call_with_retries(api: Garmin, method_name: str, *args: Any, retries: int = 2, **kwargs: Any) -> tuple[Any, str | None]:
    last_err: str | None = None

    for attempt in range(retries + 1):
        value, err = _optional_call_first(api, (method_name,), *args, **kwargs)
        if value is not None:
            return value, None

        last_err = err
        err_text = (err or "").lower()

        retryable = any(token in err_text for token in [
            "502",
            "503",
            "504",
            "bad gateway",
            "gateway timeout",
            "cloudflare",
            "temporarily unavailable",
            "connection",
            "timeout",
        ])

        if not retryable or attempt >= retries:
            return value, last_err

        _sleep_with_jitter(0.8 * (attempt + 1))

    return None, last_err


def _fetch_activity_bundle(api: Garmin, activity_id: str, include_time_series: bool = False, max_samples: int = 200) -> dict[str, Any]:
    activity, activity_err = _call_with_retries(api, "get_activity", activity_id, retries=1)
    details, details_err = _call_with_retries(api, "get_activity_details", activity_id, retries=2)
    splits, splits_err = _call_with_retries(api, "get_activity_splits", activity_id, retries=1)
    typed_splits, typed_splits_err = _call_with_retries(api, "get_activity_typed_splits", activity_id, retries=1)
    split_summaries, split_summaries_err = _call_with_retries(api, "get_activity_split_summaries", activity_id, retries=1)
    weather, weather_err = _call_with_retries(api, "get_activity_weather", activity_id, retries=1)
    hr_zones, hr_zones_err = _call_with_retries(api, "get_activity_hr_in_timezones", activity_id, retries=1)
    power_zones, power_zones_err = _call_with_retries(api, "get_activity_power_in_timezones", activity_id, retries=1)
    exercise_sets, exercise_sets_err = _call_with_retries(api, "get_activity_exercise_sets", activity_id, retries=1)
    gear, gear_err = _call_with_retries(api, "get_activity_gear", activity_id, retries=1)

    activity_type = _activity_type_key_from_payload(activity or {})
    summary = (activity or {}).get("summaryDTO") if isinstance(activity, dict) else {}
    metadata = (activity or {}).get("metadataDTO") if isinstance(activity, dict) else {}

    bundle: dict[str, Any] = {
        "activity_id": activity_id,
        "activity_name": (activity or {}).get("activityName") if isinstance(activity, dict) else None,
        "activity_type": activity_type,
        "activity_family": _activity_family(activity_type),
        "start_time_local": (summary or {}).get("startTimeLocal") or ((activity or {}).get("startTimeLocal") if isinstance(activity, dict) else None),
        "summary": _pick_activity_summary(summary),
        "metadata": _pick_activity_metadata(metadata),
        "detail_metric_descriptors": _extract_metric_descriptors(details),
        "detail_counts": _extract_detail_counts(details),
        "split_counts": _extract_split_counts(splits, typed_splits, split_summaries),
        "laps": (splits or {}).get("lapDTOs") if isinstance(splits, dict) else None,
        "events": (splits or {}).get("eventDTOs") if isinstance(splits, dict) else None,
        "typed_splits": (typed_splits or {}).get("splits") if isinstance(typed_splits, dict) else None,
        "split_summaries": (split_summaries or {}).get("splitSummaries") if isinstance(split_summaries, dict) else None,
        "weather": weather if isinstance(weather, dict) else None,
        "hr_time_in_zones": hr_zones if isinstance(hr_zones, list) else None,
        "power_time_in_zones": power_zones if isinstance(power_zones, list) else None,
        "exercise_sets": (exercise_sets or {}).get("exerciseSets") if isinstance(exercise_sets, dict) else None,
        "exercise_set_count": _exercise_set_count(exercise_sets),
        "strength_summary": _summarize_strength_sets(exercise_sets),
        "gear": gear if isinstance(gear, list) else None,
        "gear_count": len(gear) if isinstance(gear, list) else 0,
        "available_sections": {
            "activity": activity is not None,
            "details": details is not None,
            "splits": splits is not None,
            "typed_splits": typed_splits is not None,
            "split_summaries": split_summaries is not None,
            "weather": weather is not None,
            "hr_zones": hr_zones is not None,
            "power_zones": power_zones is not None,
            "exercise_sets": exercise_sets is not None,
            "gear": gear is not None,
        },
        "source_errors": {
            "activity": activity_err,
            "details": details_err,
            "splits": splits_err,
            "typed_splits": typed_splits_err,
            "split_summaries": split_summaries_err,
            "weather": weather_err,
            "hr_zones": hr_zones_err,
            "power_zones": power_zones_err,
            "exercise_sets": exercise_sets_err,
            "gear": gear_err,
        },
    }

    if include_time_series:
        bundle["time_series"] = _compact_activity_time_series(details, max_samples=max_samples)

    return bundle


@mcp.tool
def get_activity_full(activity_id: str, include_time_series: bool = False, max_samples: int = 200) -> dict[str, Any]:
    """Actividad completa con resumen, vueltas, clima, zonas, material y series temporales opcionales."""
    max_samples = max(1, min(2000, int(max_samples)))
    with FETCH_LOCK:
        api = _get_api()
        return _fetch_activity_bundle(api, str(activity_id), include_time_series=include_time_series, max_samples=max_samples)


@mcp.tool
def get_activity_time_series(activity_id: str, max_samples: int = 300) -> dict[str, Any]:
    """Serie temporal compacta de una actividad con métricas por muestra."""
    max_samples = max(1, min(2000, int(max_samples)))
    with FETCH_LOCK:
        api = _get_api()
        details, err = _call_with_retries(api, "get_activity_details", str(activity_id), retries=2)
        if details is None:
            raise RuntimeError(err or "No pude leer los detalles de la actividad")
        result = _compact_activity_time_series(details, max_samples=max_samples)
        result["activity_id"] = str(activity_id)
        result["detail_counts"] = _extract_detail_counts(details)
        return result


@mcp.tool
def get_recent_activities_full(limit: int = 8) -> list[dict[str, Any]]:
    """Actividades recientes con resumen completo, vueltas, clima, zonas, material y sets."""
    limit = max(1, min(12, int(limit)))
    with FETCH_LOCK:
        api = _get_api()
        activities, err = _optional_call_first(api, ("get_activities",), 0, limit)
        if activities is None:
            raise RuntimeError(err or "No pude leer las actividades recientes")

        activity_ids: list[str] = []
        for activity in activities[:limit]:
            if not isinstance(activity, dict):
                continue
            activity_id = activity.get("activityId")
            if activity_id is None:
                continue
            activity_ids.append(str(activity_id))

        return [_fetch_activity_bundle(api, activity_id, include_time_series=False) for activity_id in activity_ids]
# === MCPX ACTIVITY DEEP PATCH END ===


# === MCPX ALL SPORTS RAW TOOLS START ===

def _available_summary_keys(activity: Any) -> list[str]:
    if not isinstance(activity, dict):
        return []
    summary = activity.get("summaryDTO") or {}
    if not isinstance(summary, dict):
        return []
    return sorted(summary.keys())


def _available_metadata_keys(activity: Any) -> list[str]:
    if not isinstance(activity, dict):
        return []
    metadata = activity.get("metadataDTO") or {}
    if not isinstance(metadata, dict):
        return []
    return sorted(metadata.keys())


def _available_detail_metric_keys(details: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(details, dict):
        return out
    for d in details.get("metricDescriptors") or []:
        if not isinstance(d, dict):
            continue
        key = d.get("key")
        if key:
            out.append(str(key))
    return sorted(out)


def _fetch_activity_all_data(api: Garmin, activity_id: str, include_time_series: bool = False, max_samples: int = 500) -> dict[str, Any]:
    activity, activity_err = _call_with_retries(api, "get_activity", activity_id, retries=1)
    details, details_err = _call_with_retries(api, "get_activity_details", activity_id, retries=2)
    splits, splits_err = _call_with_retries(api, "get_activity_splits", activity_id, retries=1)
    typed_splits, typed_splits_err = _call_with_retries(api, "get_activity_typed_splits", activity_id, retries=1)
    split_summaries, split_summaries_err = _call_with_retries(api, "get_activity_split_summaries", activity_id, retries=1)
    weather, weather_err = _call_with_retries(api, "get_activity_weather", activity_id, retries=1)
    hr_zones, hr_zones_err = _call_with_retries(api, "get_activity_hr_in_timezones", activity_id, retries=1)
    power_zones, power_zones_err = _call_with_retries(api, "get_activity_power_in_timezones", activity_id, retries=1)
    exercise_sets, exercise_sets_err = _call_with_retries(api, "get_activity_exercise_sets", activity_id, retries=1)
    gear, gear_err = _call_with_retries(api, "get_activity_gear", activity_id, retries=1)

    compact = _fetch_activity_bundle(api, activity_id, include_time_series=include_time_series, max_samples=max_samples)

    compact["available_summary_keys"] = _available_summary_keys(activity)
    compact["available_metadata_keys"] = _available_metadata_keys(activity)
    compact["available_detail_metric_keys"] = _available_detail_metric_keys(details)

    compact["raw_payloads"] = {
        "activity_raw": activity,
        "details_raw": details,
        "splits_raw": splits,
        "typed_splits_raw": typed_splits,
        "split_summaries_raw": split_summaries,
        "weather_raw": weather,
        "hr_time_in_zones_raw": hr_zones,
        "power_time_in_zones_raw": power_zones,
        "exercise_sets_raw": exercise_sets,
        "gear_raw": gear,
    }

    compact["raw_payload_errors"] = {
        "activity": activity_err,
        "details": details_err,
        "splits": splits_err,
        "typed_splits": typed_splits_err,
        "split_summaries": split_summaries_err,
        "weather": weather_err,
        "hr_zones": hr_zones_err,
        "power_zones": power_zones_err,
        "exercise_sets": exercise_sets_err,
        "gear": gear_err,
    }

    return compact


@mcp.tool
def get_activity_all_data(activity_id: str, include_time_series: bool = False, max_samples: int = 500) -> dict[str, Any]:
    """Devuelve todos los payloads crudos y el bundle compacto de una actividad, sin filtrar por deporte."""
    max_samples = max(1, min(2000, int(max_samples)))
    with FETCH_LOCK:
        api = _get_api()
        return _fetch_activity_all_data(api, str(activity_id), include_time_series=include_time_series, max_samples=max_samples)


@mcp.tool
def get_recent_activities_catalog(limit: int = 12) -> list[dict[str, Any]]:
    """Catálogo reciente de actividades para elegir activity_id y deporte."""
    limit = max(1, min(30, int(limit)))
    with FETCH_LOCK:
        api = _get_api()
        activities, err = _optional_call_first(api, ("get_activities",), 0, limit)
        if activities is None:
            raise RuntimeError(err or "No pude leer las actividades recientes")

        out: list[dict[str, Any]] = []
        for activity in activities[:limit]:
            if not isinstance(activity, dict):
                continue
            activity_type = activity.get("activityType") or activity.get("activityTypeDTO") or {}
            out.append({
                "activity_id": activity.get("activityId"),
                "activity_name": activity.get("activityName"),
                "activity_type": activity_type.get("typeKey"),
                "activity_family": _activity_family(activity_type.get("typeKey")),
                "start_time_local": activity.get("startTimeLocal"),
            })
        return out


@mcp.tool
def get_recent_activities_all_data(limit: int = 3, include_time_series: bool = False, max_samples: int = 300) -> list[dict[str, Any]]:
    """Devuelve todos los payloads crudos para varias actividades recientes. Úsalo con límites pequeños."""
    limit = max(1, min(8, int(limit)))
    max_samples = max(1, min(2000, int(max_samples)))
    with FETCH_LOCK:
        api = _get_api()
        activities, err = _optional_call_first(api, ("get_activities",), 0, limit)
        if activities is None:
            raise RuntimeError(err or "No pude leer las actividades recientes")

        out: list[dict[str, Any]] = []
        for activity in activities[:limit]:
            if not isinstance(activity, dict):
                continue
            activity_id = activity.get("activityId")
            if activity_id is None:
                continue
            out.append(_fetch_activity_all_data(api, str(activity_id), include_time_series=include_time_series, max_samples=max_samples))
        return out

# === MCPX ALL SPORTS RAW TOOLS END ===


# === MCPX SPORT PROFILE TOOLS START ===

def _pick_keys(source: Any, keys: list[str]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {k: source.get(k) for k in keys if source.get(k) is not None}


def _sport_profile_running_like(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    return {
        "sport_profile_type": "running_like",
        "primary_metrics": _pick_keys(summary, [
            "distance",
            "duration",
            "elapsedDuration",
            "movingDuration",
            "averageHR",
            "maxHR",
            "averageSpeed",
            "averageMovingSpeed",
            "maxSpeed",
            "averagePower",
            "maxPower",
            "normalizedPower",
            "averageRunCadence",
            "maxRunCadence",
            "groundContactTime",
            "verticalOscillation",
            "verticalRatio",
            "strideLength",
            "steps",
            "elevationGain",
            "elevationLoss",
            "avgElevation",
            "averageTemperature",
            "maxTemperature",
            "minTemperature",
            "activityTrainingLoad",
            "trainingEffect",
            "anaerobicTrainingEffect",
            "trainingEffectLabel",
            "beginPotentialStamina",
            "endPotentialStamina",
            "minAvailableStamina",
            "moderateIntensityMinutes",
            "vigorousIntensityMinutes",
            "waterEstimated",
        ]),
        "detail_metric_keys": bundle.get("available_detail_metric_keys") or [],
        "detail_counts": bundle.get("detail_counts") or {},
        "split_counts": bundle.get("split_counts") or {},
        "hr_time_in_zones": bundle.get("hr_time_in_zones"),
        "power_time_in_zones": bundle.get("power_time_in_zones"),
        "weather": bundle.get("weather"),
        "gear": bundle.get("gear"),
    }


def _sport_profile_strength(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    strength_summary = bundle.get("strength_summary") or {}
    return {
        "sport_profile_type": "strength",
        "primary_metrics": _pick_keys(summary, [
            "duration",
            "elapsedDuration",
            "movingDuration",
            "averageHR",
            "maxHR",
            "averageTemperature",
            "maxTemperature",
            "minTemperature",
            "activityTrainingLoad",
            "trainingEffect",
            "anaerobicTrainingEffect",
            "trainingEffectLabel",
            "moderateIntensityMinutes",
            "vigorousIntensityMinutes",
            "calories",
            "waterEstimated",
            "steps",
        ]),
        "strength_summary": strength_summary,
        "hr_time_in_zones": bundle.get("hr_time_in_zones"),
        "weather": bundle.get("weather"),
    }


def _sport_profile_cycling(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    return {
        "sport_profile_type": "cycling",
        "primary_metrics": _pick_keys(summary, [
            "distance",
            "duration",
            "elapsedDuration",
            "movingDuration",
            "averageHR",
            "maxHR",
            "averageSpeed",
            "averageMovingSpeed",
            "maxSpeed",
            "averagePower",
            "maxPower",
            "normalizedPower",
            "totalWork",
            "elevationGain",
            "elevationLoss",
            "avgElevation",
            "averageTemperature",
            "activityTrainingLoad",
            "trainingEffect",
            "anaerobicTrainingEffect",
            "trainingEffectLabel",
            "moderateIntensityMinutes",
            "vigorousIntensityMinutes",
            "waterEstimated",
        ]),
        "detail_metric_keys": bundle.get("available_detail_metric_keys") or [],
        "hr_time_in_zones": bundle.get("hr_time_in_zones"),
        "power_time_in_zones": bundle.get("power_time_in_zones"),
        "weather": bundle.get("weather"),
        "gear": bundle.get("gear"),
    }


def _sport_profile_swimming(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    return {
        "sport_profile_type": "swimming",
        "primary_metrics": _pick_keys(summary, [
            "distance",
            "duration",
            "elapsedDuration",
            "movingDuration",
            "averageHR",
            "maxHR",
            "averageSpeed",
            "maxSpeed",
            "calories",
            "activityTrainingLoad",
            "trainingEffect",
            "anaerobicTrainingEffect",
            "trainingEffectLabel",
            "moderateIntensityMinutes",
            "vigorousIntensityMinutes",
        ]),
        "detail_metric_keys": bundle.get("available_detail_metric_keys") or [],
        "hr_time_in_zones": bundle.get("hr_time_in_zones"),
    }


def _sport_profile_transport(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    return {
        "sport_profile_type": "transport",
        "primary_metrics": _pick_keys(summary, [
            "distance",
            "duration",
            "elapsedDuration",
            "movingDuration",
            "averageSpeed",
            "maxSpeed",
            "calories",
            "steps",
        ]),
        "note": "Actividad informativa, no perfil principal de entrenamiento."
    }


def _sport_profile_other(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    return {
        "sport_profile_type": "other",
        "primary_metrics": summary,
        "detail_metric_keys": bundle.get("available_detail_metric_keys") or [],
        "hr_time_in_zones": bundle.get("hr_time_in_zones"),
        "power_time_in_zones": bundle.get("power_time_in_zones"),
        "weather": bundle.get("weather"),
        "gear": bundle.get("gear"),
    }


def _build_sport_profile(bundle: dict[str, Any]) -> dict[str, Any]:
    activity_type = bundle.get("activity_type")
    family = bundle.get("activity_family")

    if family == "strength":
        return _sport_profile_strength(bundle)
    if family == "cycling":
        return _sport_profile_cycling(bundle)
    if family == "swimming":
        return _sport_profile_swimming(bundle)
    if family == "transport":
        return _sport_profile_transport(bundle)
    if family == "endurance" or activity_type in {"walking", "cardio", "elliptical", "treadmill_running", "running", "hiking"}:
        return _sport_profile_running_like(bundle)
    return _sport_profile_other(bundle)


@mcp.tool
def get_activity_sport_profile(activity_id: str, include_time_series: bool = False, max_samples: int = 300) -> dict[str, Any]:
    """Perfil interpretado por deporte usando todos los datos disponibles de la actividad."""
    max_samples = max(1, min(2000, int(max_samples)))
    with FETCH_LOCK:
        api = _get_api()
        bundle = _fetch_activity_all_data(api, str(activity_id), include_time_series=include_time_series, max_samples=max_samples)
        profile = _build_sport_profile(bundle)
        return {
            "activity_id": bundle.get("activity_id"),
            "activity_name": bundle.get("activity_name"),
            "activity_type": bundle.get("activity_type"),
            "activity_family": bundle.get("activity_family"),
            "start_time_local": bundle.get("start_time_local"),
            "sport_profile": profile,
            "source_errors": bundle.get("source_errors"),
            "raw_payload_errors": bundle.get("raw_payload_errors"),
        }


@mcp.tool
def get_recent_activity_sport_profiles(limit: int = 8) -> list[dict[str, Any]]:
    """Perfiles interpretados por deporte para actividades recientes."""
    limit = max(1, min(12, int(limit)))
    with FETCH_LOCK:
        api = _get_api()
        activities, err = _optional_call_first(api, ("get_activities",), 0, limit)
        if activities is None:
            raise RuntimeError(err or "No pude leer las actividades recientes")

        out: list[dict[str, Any]] = []
        for activity in activities[:limit]:
            if not isinstance(activity, dict):
                continue
            activity_id = activity.get("activityId")
            if activity_id is None:
                continue
            bundle = _fetch_activity_all_data(api, str(activity_id), include_time_series=False, max_samples=300)
            out.append({
                "activity_id": bundle.get("activity_id"),
                "activity_name": bundle.get("activity_name"),
                "activity_type": bundle.get("activity_type"),
                "activity_family": bundle.get("activity_family"),
                "start_time_local": bundle.get("start_time_local"),
                "sport_profile": _build_sport_profile(bundle),
                "source_errors": bundle.get("source_errors"),
                "raw_payload_errors": bundle.get("raw_payload_errors"),
            })
        return out

# === MCPX SPORT PROFILE TOOLS END ===


# === MCPX VISIBLE METRICS PATCH START ===

def _raw_summary_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    raw_payloads = bundle.get("raw_payloads") or {}
    activity_raw = raw_payloads.get("activity_raw") or {}
    summary = activity_raw.get("summaryDTO") or {}
    return summary if isinstance(summary, dict) else {}


def _format_speed_as_pace(value: Any) -> str | None:
    try:
        mps = float(value)
        if mps <= 0:
            return None
        total_seconds = 1000.0 / mps
        minutes = int(total_seconds // 60)
        seconds = int(round(total_seconds % 60))
        if seconds == 60:
            minutes += 1
            seconds = 0
        return f"{minutes}:{seconds:02d} /km"
    except Exception:
        return None


def _format_celsius(value: Any) -> str | None:
    try:
        return f"{float(value):.1f} °C"
    except Exception:
        return None


def _format_meters(value: Any) -> str | None:
    try:
        return f"{float(value):.2f} m"
    except Exception:
        return None


def _format_centimeters(value: Any) -> str | None:
    try:
        return f"{float(value):.2f} cm"
    except Exception:
        return None


def _format_milliseconds(value: Any) -> str | None:
    try:
        return f"{float(value):.1f} ms"
    except Exception:
        return None


def _format_spm(value: Any) -> str | None:
    try:
        return f"{float(value):.1f} spm"
    except Exception:
        return None


def _format_watts(value: Any) -> str | None:
    try:
        return f"{float(value):.0f} W"
    except Exception:
        return None


def _format_kilograms(value: Any) -> str | None:
    try:
        return f"{float(value):.0f} kg"
    except Exception:
        return None


def _format_ppm(value: Any) -> str | None:
    try:
        return f"{float(value):.0f} ppm"
    except Exception:
        return None


def _format_percent_plain(value: Any) -> str | None:
    try:
        return f"{float(value):.2f}"
    except Exception:
        return None


def _drop_none_deep(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            cleaned = _drop_none_deep(v)
            if cleaned is None:
                continue
            if cleaned == {} or cleaned == []:
                continue
            out[k] = cleaned
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            cleaned = _drop_none_deep(item)
            if cleaned is None:
                continue
            out.append(cleaned)
        return out
    return value


def _training_label_es(value: Any) -> Any:
    mapping = {
        "VO2MAX": "VO2 máximo",
        "LACTATE_THRESHOLD": "Umbral de lactato",
        "TEMPO": "Tempo",
        "ANAEROBIC_CAPACITY": "Capacidad anaeróbica",
        "AEROBIC_BASE": "Base aeróbica",
        "RECOVERY": "Recuperación",
        "THRESHOLD": "Umbral",
    }
    if value is None:
        return None
    return mapping.get(str(value), value)


def _format_seconds_mmss(value: Any) -> str | None:
    try:
        total = int(round(float(value)))
    except Exception:
        return None
    m = total // 60
    s = total % 60
    return f"{m}:{s:02d}"


def _format_percentage(value: Any) -> str | None:
    try:
        return f"{float(value):.0f}%"
    except Exception:
        return None


def _hr_zone_label_es(zone_number: Any) -> str | None:
    mapping = {
        1: "Calentamiento",
        2: "Suave",
        3: "Aeróbica",
        4: "Umbral",
        5: "Máximo",
    }
    try:
        return mapping.get(int(zone_number))
    except Exception:
        return None


def _power_zone_label_es(zone_number: Any) -> str | None:
    mapping = {
        1: "Fácil",
        2: "Moderado",
        3: "Tempo",
        4: "Intervalo largo",
        5: "Intervalo corto",
    }
    try:
        return mapping.get(int(zone_number))
    except Exception:
        return None


def _format_zone_rows(rows: Any, zone_type: str = "hr") -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []

    total_secs = 0.0
    for row in rows:
        if isinstance(row, dict):
            try:
                total_secs += float(row.get("secsInZone") or 0)
            except Exception:
                pass

    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        zone_number = row.get("zoneNumber")
        secs = row.get("secsInZone")
        low = row.get("zoneLowBoundary")

        try:
            pct = (float(secs) / total_secs * 100.0) if total_secs > 0 else 0.0
        except Exception:
            pct = None

        label = _hr_zone_label_es(zone_number) if zone_type == "hr" else _power_zone_label_es(zone_number)

        out.append({
            "Zona": f"Zona {zone_number}" if zone_number is not None else None,
            "Límite inferior": low,
            "Etiqueta": label,
            "Tiempo": _format_seconds_mmss(secs),
            "Porcentaje": _format_percentage(pct) if pct is not None else None,
            "secs_raw": secs,
        })

    return out


def _format_distance_km(value: Any) -> str | None:
    try:
        return f"{float(value)/1000.0:.2f} km"
    except Exception:
        return None


def _format_distance_km_plain(value: Any) -> float | None:
    try:
        return round(float(value) / 1000.0, 2)
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _extract_lap_list(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    laps = bundle.get("laps")
    return laps if isinstance(laps, list) else []


def _visible_laps_or_segments(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    laps = _extract_lap_list(bundle)
    out = []

    for idx, lap in enumerate(laps, 1):
        if not isinstance(lap, dict):
            continue

        out.append({
            "Vuelta": idx,
            "Tiempo": _format_seconds_mmss(lap.get("duration")),
            "Tiempo acumulado": _format_seconds_mmss(lap.get("elapsedDuration")),
            "Distancia": _format_distance_km(lap.get("distance")),
            "Ritmo medio": _format_speed_as_pace(lap.get("averageSpeed")),
            "GAP medio": _format_speed_as_pace(lap.get("averageGradeAdjustedSpeed")),
            "Frecuencia cardiaca media": _format_ppm(lap.get("averageHR")),
            "FC máxima": _format_ppm(lap.get("maxHR")),
            "Ascenso total": _format_meters(lap.get("elevationGain")),
            "Descenso total": _format_meters(lap.get("elevationLoss")),
            "Potencia media": _format_watts(lap.get("averagePower")),
            "Potencia máxima": _format_watts(lap.get("maxPower")),
            "Cadencia de carrera media": _format_spm(lap.get("averageRunCadence")),
            "Cadencia de carrera máxima": _format_spm(lap.get("maxRunCadence")),
        })

    return _drop_none_deep(out)


def _seconds_to_hms(seconds: Any) -> str | None:
    try:
        total = int(round(float(seconds)))
    except Exception:
        return None
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _weighted_intensity_total(moderate: Any, vigorous: Any) -> float | None:
    try:
        mod = float(moderate or 0)
        vig = float(vigorous or 0)
        return mod + (vig * 2)
    except Exception:
        return None


def _visible_metrics_running_like(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    result = {
        "Altura": {
            "Altura media": _format_meters(summary.get("avgElevation")),
            "Altura máxima": _format_meters(summary.get("maxElevation")),
            "Altura mínima": _format_meters(summary.get("minElevation")),
            "Ascenso total": _format_meters(summary.get("elevationGain")),
            "Descenso total": _format_meters(summary.get("elevationLoss")),
        },
        "Ritmo": {
            "Ritmo medio": _format_speed_as_pace(summary.get("averageSpeed")),
            "Ritmo en movimiento": _format_speed_as_pace(summary.get("averageMovingSpeed")),
            "Ritmo máximo": _format_speed_as_pace(summary.get("maxSpeed")),
            "Ritmo ajustado por pendiente": _format_speed_as_pace(summary.get("avgGradeAdjustedSpeed")),
        },
        "Frecuencia cardiaca": {
            "Frecuencia cardiaca media": _format_ppm(summary.get("averageHR")),
            "Frecuencia cardiaca máxima": _format_ppm(summary.get("maxHR")),
            "Frecuencia cardiaca mínima": _format_ppm(summary.get("minHR")),
            "Tiempo de las zonas": _format_zone_rows(bundle.get("hr_time_in_zones"), zone_type="hr"),
        },
        "Condición de rendimiento": {
            "Serie temporal disponible": "directPerformanceCondition" in (bundle.get("available_detail_metric_keys") or []),
        },
        "Longitud de zancada": {
            "Longitud de zancada media": _format_centimeters(summary.get("strideLength")),
        },
        "Cadencia de carrera": {
            "Cadencia de carrera media": _format_spm(summary.get("averageRunCadence")),
            "Cadencia de carrera máxima": _format_spm(summary.get("maxRunCadence")),
        },
        "Potencia: Vatios": {
            "Potencia media": _format_watts(summary.get("averagePower")),
            "Potencia máxima": _format_watts(summary.get("maxPower")),
            "Potencia normalizada": _format_watts(summary.get("normalizedPower")),
            "Trabajo total": summary.get("totalWork"),
            "Tiempo de las zonas": _format_zone_rows(bundle.get("power_time_in_zones"), zone_type="power"),
        },
        "Ratio vertical": {
            "Ratio vertical medio": _format_percent_plain(summary.get("verticalRatio")),
        },
        "Tiempo de contacto con el suelo": {
            "Tiempo de contacto con el suelo medio": _format_milliseconds(summary.get("groundContactTime")),
        },
        "Temperatura": {
            "Temperatura media": _format_celsius(summary.get("averageTemperature")),
            "Temperatura mínima": _format_celsius(summary.get("minTemperature")),
            "Temperatura máxima": _format_celsius(summary.get("maxTemperature")),
        },
        "Energía disponible": {
            "Energía disponible mínima": summary.get("minAvailableStamina"),
        },
        "Energía disponible potencial": {
            "Energía disponible potencial al inicio": summary.get("beginPotentialStamina"),
            "Energía disponible potencial al final": summary.get("endPotentialStamina"),
        },
        "Vueltas": _visible_laps_or_segments(bundle),
    }
    return _drop_none_deep(result)


def _visible_metrics_strength(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    raw_summary = _raw_summary_from_bundle(bundle)
    strength_summary = bundle.get("strength_summary") or {}

    moderate = summary.get("moderateIntensityMinutes")
    vigorous = summary.get("vigorousIntensityMinutes")
    intensity_total = _weighted_intensity_total(moderate, vigorous)

    total_time_s = summary.get("duration")
    active_time_s = strength_summary.get("active_time_s_estimated")
    rest_time_s = strength_summary.get("rest_time_s_estimated")

    total_calories = raw_summary.get("calories", summary.get("calories"))
    resting_calories = raw_summary.get("bmrCalories")
    active_calories = None
    try:
        if total_calories is not None and resting_calories is not None:
            active_calories = float(total_calories) - float(resting_calories)
    except Exception:
        active_calories = None

    result = {
        "Tiempo": {
            "Tiempo total": _seconds_to_hms(total_time_s),
            "Tiempo de trabajo": _seconds_to_hms(active_time_s),
            "Tiempo de descanso": _seconds_to_hms(rest_time_s),
        },
        "Efecto de entrenamiento": {
            "Beneficio principal": _training_label_es(summary.get("trainingEffectLabel")),
            "Aeróbica": summary.get("trainingEffect"),
            "Anaeróbica": summary.get("anaerobicTrainingEffect"),
            "Carga de ejercicio": round(float(summary.get("activityTrainingLoad")), 0) if summary.get("activityTrainingLoad") is not None else None,
        },
        "Frecuencia cardiaca": {
            "Frecuencia cardiaca media": _format_ppm(summary.get("averageHR")),
            "FC máxima": _format_ppm(summary.get("maxHR")),
            "Tiempo de las zonas": _format_zone_rows(bundle.get("hr_time_in_zones"), zone_type="hr"),
        },
        "Detalles de la sesión de entrenamiento": {
            "Repeticiones totales": round(float(strength_summary.get("total_reps_estimated")), 0) if strength_summary.get("total_reps_estimated") is not None else None,
            "Series totales": strength_summary.get("active_sets_count_estimated"),
            "Volumen": _format_kilograms(strength_summary.get("total_volume_kg_estimated")),
            "Peso máximo visto": _format_kilograms(strength_summary.get("max_weight_kg_seen")),
            "Bloques de trabajo": [
                {
                    "series": item.get("active_sets") or item.get("sets"),
                    "repeticiones": round(float(item.get("reps")), 0) if item.get("reps") is not None else None,
                    "peso máximo": _format_kilograms(item.get("max_weight_kg")),
                    "volumen": _format_kilograms(item.get("volume_kg")),
                }
                for item in (strength_summary.get("top_exercises") or [])
            ],
        },
        "Temperatura": {
            "Temperatura media": _format_celsius(summary.get("averageTemperature")),
            "Temperatura mínima": _format_celsius(summary.get("minTemperature")),
            "Temperatura máxima": _format_celsius(summary.get("maxTemperature")),
        },
        "Minutos de intensidad": {
            "Moderado": moderate,
            "Alta": vigorous,
            "Total": intensity_total,
        },
        "Body Battery": {
            "Impacto neto": summary.get("differenceBodyBattery"),
        },
        "Nutrición e hidratación": {
            "Calorías en reposo": resting_calories,
            "Calorías activas": active_calories,
            "Total de calorías quemadas": total_calories,
            "Pérdida estimada de líquidos": summary.get("waterEstimated"),
        },
    }
    return _drop_none_deep(result)


def _visible_metrics_cycling(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    return {
        "Altura": {
            "Altura media": summary.get("avgElevation"),
            "Altura máxima": summary.get("maxElevation"),
            "Altura mínima": summary.get("minElevation"),
            "Ascenso total": summary.get("elevationGain"),
            "Descenso total": summary.get("elevationLoss"),
        },
        "Velocidad": {
            "Velocidad media": summary.get("averageSpeed"),
            "Velocidad en movimiento": summary.get("averageMovingSpeed"),
            "Velocidad máxima": summary.get("maxSpeed"),
        },
        "Frecuencia cardiaca": {
            "Frecuencia cardiaca media": summary.get("averageHR"),
            "Frecuencia cardiaca máxima": summary.get("maxHR"),
            "Tiempo de las zonas": bundle.get("hr_time_in_zones"),
        },
        "Potencia: Vatios": {
            "Potencia media": summary.get("averagePower"),
            "Potencia máxima": summary.get("maxPower"),
            "Potencia normalizada": summary.get("normalizedPower"),
            "Trabajo total": summary.get("totalWork"),
            "Tiempo de las zonas": bundle.get("power_time_in_zones"),
        },
        "Temperatura": {
            "Temperatura media": summary.get("averageTemperature"),
            "Temperatura mínima": summary.get("minTemperature"),
            "Temperatura máxima": summary.get("maxTemperature"),
        },
    }


def _visible_metrics_swimming(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    return {
        "Ritmo": {
            "Ritmo medio": summary.get("averageSpeed"),
            "Ritmo máximo": summary.get("maxSpeed"),
        },
        "Frecuencia cardiaca": {
            "Frecuencia cardiaca media": summary.get("averageHR"),
            "Frecuencia cardiaca máxima": summary.get("maxHR"),
            "Tiempo de las zonas": bundle.get("hr_time_in_zones"),
        },
        "Carga": {
            "Carga de ejercicio": summary.get("activityTrainingLoad"),
            "Efecto de entrenamiento aeróbico": summary.get("trainingEffect"),
            "Efecto de entrenamiento anaeróbico": summary.get("anaerobicTrainingEffect"),
            "Beneficio principal": summary.get("trainingEffectLabel"),
        },
    }


def _format_km(value: Any) -> str | None:
    try:
        return f"{float(value)/1000.0:.2f} km"
    except Exception:
        return None


def _format_kmh(value: Any) -> str | None:
    try:
        return f"{float(value)*3.6:.1f} km/h"
    except Exception:
        return None


def _format_plain_minutes(value: Any) -> Any:
    try:
        return int(round(float(value)))
    except Exception:
        return value


def _get_first_typed_split_of_type(bundle: dict[str, Any], split_type: str) -> dict[str, Any] | None:
    typed = bundle.get("typed_splits")
    if not isinstance(typed, list):
        return None
    for item in typed:
        if not isinstance(item, dict):
            continue
        if str(item.get("splitType") or "").upper() == split_type.upper():
            return item
    return None


def _sum_typed_split_duration(bundle: dict[str, Any], split_type: str) -> float | None:
    typed = bundle.get("typed_splits")
    if not isinstance(typed, list):
        return None
    total = 0.0
    found = False
    for item in typed:
        if not isinstance(item, dict):
            continue
        if str(item.get("splitType") or "").upper() != split_type.upper():
            continue
        try:
            total += float(item.get("duration") or 0)
            found = True
        except Exception:
            pass
    return total if found else None


def _raw_activity_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    raw_payloads = bundle.get("raw_payloads") or {}
    activity_raw = raw_payloads.get("activity_raw") or {}
    return activity_raw if isinstance(activity_raw, dict) else {}


def _format_execution_score(raw_activity: dict[str, Any]) -> Any:
    for key in ("userProficiency", "executionScore", "skillLevel"):
        if raw_activity.get(key) is not None:
            return raw_activity.get(key)
    return None


def _visible_metrics_endurance_full(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    raw_summary = _raw_summary_from_bundle(bundle).get("summaryDTO") or {}
    raw_activity = _raw_activity_from_bundle(bundle)

    total_calories = raw_summary.get("calories", summary.get("calories"))
    resting_calories = raw_summary.get("bmrCalories")
    active_calories = None
    net_calories = None
    try:
        if total_calories is not None and resting_calories is not None:
            active_calories = float(total_calories) - float(resting_calories)
            net_calories = -float(total_calories)
    except Exception:
        pass

    liquid_loss = summary.get("waterEstimated")
    liquid_net = None
    try:
        if liquid_loss is not None:
            liquid_net = -float(liquid_loss)
    except Exception:
        pass

    moderate = summary.get("moderateIntensityMinutes")
    vigorous = summary.get("vigorousIntensityMinutes")
    intensity_total = _weighted_intensity_total(moderate, vigorous)

    running_time = _sum_typed_split_duration(bundle, "RUN")
    walking_time = _sum_typed_split_duration(bundle, "WALK")
    inactive_time = _sum_typed_split_duration(bundle, "INACTIVE")

    interval_run = _get_first_typed_split_of_type(bundle, "INTERVAL_ACTIVE")
    if interval_run is None:
        interval_run = _get_first_typed_split_of_type(bundle, "RUN")

    result = {
        "Distancia": {
            "Distancia": _format_km(summary.get("distance")),
        },
        "Nutrición e hidratación": {
            "Calorías en reposo": resting_calories,
            "Calorías activas": active_calories,
            "Total de calorías quemadas": total_calories,
            "Calorías consumidas": None,
            "Calorías netas": net_calories,
            "Pérdida estimada de líquidos": liquid_loss,
            "Líquido ingerido": None,
            "Líquido neto": liquid_net,
        },
        "Puntuación de ejecución": {
            "Puntuación": _format_execution_score(raw_activity),
        },
        "Autoevaluación": {
            "Cómo te has sentido": raw_activity.get("userFeedback") or raw_activity.get("feel") or raw_activity.get("perceivedExerciseFeedback"),
            "Nivel de esfuerzo percibido": raw_activity.get("perceivedExerciseIntensity") or raw_activity.get("rpe") or raw_activity.get("userRpe"),
        },
        "Energía disponible": {
            "Potencial inicial": summary.get("beginPotentialStamina"),
            "Potencial final": summary.get("endPotentialStamina"),
            "Energía disponible mín.": summary.get("minAvailableStamina"),
        },
        "Efecto de entrenamiento": {
            "Beneficio principal": _training_label_es(summary.get("trainingEffectLabel")),
            "Aeróbica": summary.get("trainingEffect"),
            "Anaeróbica": summary.get("anaerobicTrainingEffect"),
            "Carga de ejercicio": round(float(summary.get("activityTrainingLoad")), 0) if summary.get("activityTrainingLoad") is not None else None,
        },
        "Potencia": {
            "Potencia media": _format_watts(summary.get("averagePower")),
            "Potencia máxima": _format_watts(summary.get("maxPower")),
            "Datos del viento": "Activado" if raw_activity.get("metadataDTO", {}).get("hasRunPowerWindData") else "Desactivado",
        },
        "Altura": {
            "Ascenso total": _format_meters(summary.get("elevationGain")),
            "Descenso total": _format_meters(summary.get("elevationLoss")),
            "Altura mínima": _format_meters(summary.get("minElevation")),
            "Altura máxima": _format_meters(summary.get("maxElevation")),
        },
        "Frecuencia cardiaca": {
            "ppM": {
                "Frecuencia cardiaca media": _format_ppm(summary.get("averageHR")),
                "FC máxima": _format_ppm(summary.get("maxHR")),
            },
            "% de máxima": {
                "Frecuencia cardiaca media": None,
                "FC máxima": None,
            },
            "Zonas": _format_zone_rows(bundle.get("hr_time_in_zones"), zone_type="hr"),
        },
        "Tiempo": {
            "Tiempo": _seconds_to_hms(summary.get("duration")),
            "Tiempo en movimiento": _seconds_to_hms(summary.get("movingDuration")),
            "Tiempo transcurrido": _seconds_to_hms(summary.get("elapsedDuration")),
        },
        "Detección de carrera/caminar": {
            "Tiempo de carrera": _seconds_to_hms(running_time),
            "Tiempo de caminar": _seconds_to_hms(walking_time),
            "Tiempo de inactividad": _seconds_to_hms(inactive_time),
        },
        "Ritmo/velocidad": {
            "Ritmo": {
                "Ritmo medio": _format_speed_as_pace(summary.get("averageSpeed")),
                "Ritmo medio en movimiento": _format_speed_as_pace(summary.get("averageMovingSpeed")),
                "Ritmo óptimo": _format_speed_as_pace(summary.get("maxSpeed")),
                "Ritmo medio adaptado a la pendiente": _format_speed_as_pace(summary.get("avgGradeAdjustedSpeed")),
            },
            "Velocidad": {
                "Velocidad media": _format_kmh(summary.get("averageSpeed")),
                "Velocidad media en movimiento": _format_kmh(summary.get("averageMovingSpeed")),
                "Velocidad máxima": _format_kmh(summary.get("maxSpeed")),
                "Velocidad media adaptada a la pendiente": _format_kmh(summary.get("avgGradeAdjustedSpeed")),
            },
        },
        "Intervalos de entrenamiento": {
            "Tiempo: Carrera": _seconds_to_hms(interval_run.get("duration") if isinstance(interval_run, dict) else None),
            "Distancia: Carrera": _format_km(interval_run.get("distance") if isinstance(interval_run, dict) else None),
            "Carrera Ritmo": _format_speed_as_pace(interval_run.get("averageSpeed") if isinstance(interval_run, dict) else None),
        },
        "Dinámica de carrera": {
            "Cadencia de carrera media": _format_spm(summary.get("averageRunCadence")),
            "Cadencia de carrera máxima": _format_spm(summary.get("maxRunCadence")),
            "Longitud media de zancada": _format_centimeters(summary.get("strideLength")),
            "Relación vertical media": _format_percent_plain(summary.get("verticalRatio")),
            "Oscilación vertical media": _format_centimeters(summary.get("verticalOscillation")),
            "Tiempo medio de contacto con el suelo": _format_milliseconds(summary.get("groundContactTime")),
        },
        "Temperatura": {
            "Temperatura media": _format_celsius(summary.get("averageTemperature")),
            "Temperatura mínima": _format_celsius(summary.get("minTemperature")),
            "Temperatura máxima": _format_celsius(summary.get("maxTemperature")),
        },
        "Minutos de intensidad": {
            "Moderado": moderate,
            "Alta": vigorous,
            "Total": intensity_total,
        },
        "Body Battery": {
            "Impacto neto": summary.get("differenceBodyBattery"),
        },
        "Vueltas": _visible_laps_or_segments(bundle),
        "Zonas de potencia": _format_zone_rows(bundle.get("power_time_in_zones"), zone_type="power"),
    }

    return _drop_none_deep(result)


def _build_visible_metrics(bundle: dict[str, Any]) -> dict[str, Any]:
    family = bundle.get("activity_family")
    activity_type = bundle.get("activity_type")

    if family == "strength":
        return _visible_metrics_strength(bundle)
    if family == "cycling":
        return _visible_metrics_cycling(bundle)
    if family == "swimming":
        return _visible_metrics_swimming(bundle)
    if family == "endurance" or activity_type in {"running", "treadmill_running", "walking", "hiking", "cardio", "elliptical"}:
        return _visible_metrics_endurance_full(bundle)
    return {}


@mcp.tool
def get_activity_visible_profile(activity_id: str, include_time_series: bool = False, max_samples: int = 300) -> dict[str, Any]:
    """Perfil con nombres visibles estilo Garmin Connect según el tipo de actividad."""
    max_samples = max(1, min(2000, int(max_samples)))
    with FETCH_LOCK:
        api = _get_api()
        bundle = _fetch_activity_all_data(api, str(activity_id), include_time_series=include_time_series, max_samples=max_samples)
        return {
            "activity_id": bundle.get("activity_id"),
            "activity_name": bundle.get("activity_name"),
            "activity_type": bundle.get("activity_type"),
            "activity_family": bundle.get("activity_family"),
            "start_time_local": bundle.get("start_time_local"),
            "visible_metrics": _build_visible_metrics(bundle),
            "source_errors": bundle.get("source_errors"),
            "raw_payload_errors": bundle.get("raw_payload_errors"),
        }

# === MCPX VISIBLE METRICS PATCH END ===


# === MCPX HYBRID OVERVIEW TOOL START ===

def _num_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _round_or_none(value: Any, ndigits: int = 1) -> float | None:
    num = _num_or_none(value)
    if num is None:
        return None
    return round(num, ndigits)


def _duration_min_from_summary(summary: dict[str, Any]) -> float | None:
    if not isinstance(summary, dict):
        return None
    duration = _num_or_none(summary.get("duration"))
    if duration is None:
        return None
    return round(duration / 60.0, 1)


def _distance_km_from_summary(summary: dict[str, Any]) -> float | None:
    if not isinstance(summary, dict):
        return None
    distance = _num_or_none(summary.get("distance"))
    if distance is None:
        return None
    return round(distance / 1000.0, 2)


def _bundle_hybrid_session(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("summary") or {}
    strength_summary = bundle.get("strength_summary") or {}

    return {
        "activity_id": bundle.get("activity_id"),
        "activity_name": bundle.get("activity_name"),
        "activity_type": bundle.get("activity_type"),
        "activity_family": bundle.get("activity_family"),
        "start_time_local": bundle.get("start_time_local"),
        "duration_min": _duration_min_from_summary(summary),
        "distance_km": _distance_km_from_summary(summary),
        "training_load": _round_or_none(summary.get("activityTrainingLoad"), 1),
        "training_effect_aerobic": _round_or_none(summary.get("trainingEffect"), 1),
        "training_effect_anaerobic": _round_or_none(summary.get("anaerobicTrainingEffect"), 1),
        "training_effect_label": summary.get("trainingEffectLabel"),
        "average_hr": _round_or_none(summary.get("averageHR"), 0),
        "max_hr": _round_or_none(summary.get("maxHR"), 0),
        "average_power": _round_or_none(summary.get("averagePower"), 0),
        "normalized_power": _round_or_none(summary.get("normalizedPower"), 0),
        "average_run_cadence": _round_or_none(summary.get("averageRunCadence"), 1),
        "ground_contact_time_ms": _round_or_none(summary.get("groundContactTime"), 1),
        "vertical_oscillation_cm": _round_or_none(summary.get("verticalOscillation"), 2),
        "vertical_ratio": _round_or_none(summary.get("verticalRatio"), 2),
        "stride_length_cm": _round_or_none(summary.get("strideLength"), 2),
        "stamina_begin": _round_or_none(summary.get("beginPotentialStamina"), 0),
        "stamina_end": _round_or_none(summary.get("endPotentialStamina"), 0),
        "stamina_min": _round_or_none(summary.get("minAvailableStamina"), 0),
        "moderate_intensity_minutes": _round_or_none(summary.get("moderateIntensityMinutes"), 0),
        "vigorous_intensity_minutes": _round_or_none(summary.get("vigorousIntensityMinutes"), 0),
        "exercise_set_count": bundle.get("exercise_set_count"),
        "active_sets_estimated": strength_summary.get("active_sets_count_estimated"),
        "total_reps_estimated": _round_or_none(strength_summary.get("total_reps_estimated"), 0),
        "total_volume_kg_estimated": _round_or_none(strength_summary.get("total_volume_kg_estimated"), 0),
        "max_weight_kg_seen": _round_or_none(strength_summary.get("max_weight_kg_seen"), 0),
        "source_errors": bundle.get("source_errors"),
    }


def _accumulate_type_totals(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, dict[str, Any]] = {}

    for item in sessions:
        activity_type = item.get("activity_type") or "unknown"
        row = by_type.setdefault(activity_type, {
            "activity_type": activity_type,
            "activity_family": item.get("activity_family"),
            "sessions": 0,
            "total_duration_min": 0.0,
            "total_distance_km": 0.0,
            "total_training_load": 0.0,
        })

        row["sessions"] += 1
        row["total_duration_min"] += float(item.get("duration_min") or 0.0)
        row["total_distance_km"] += float(item.get("distance_km") or 0.0)
        row["total_training_load"] += float(item.get("training_load") or 0.0)

    out = []
    for row in by_type.values():
        out.append({
            "activity_type": row["activity_type"],
            "activity_family": row["activity_family"],
            "sessions": row["sessions"],
            "total_duration_min": round(row["total_duration_min"], 1),
            "total_distance_km": round(row["total_distance_km"], 2),
            "total_training_load": round(row["total_training_load"], 1),
        })

    out.sort(key=lambda x: (-x["total_training_load"], x["activity_type"]))
    return {
        "by_type": out,
        "total_sessions": sum(x["sessions"] for x in out),
        "total_duration_min": round(sum(x["total_duration_min"] for x in out), 1),
        "total_distance_km": round(sum(x["total_distance_km"] for x in out), 2),
        "total_training_load": round(sum(x["total_training_load"] for x in out), 1),
    }


@mcp.tool
def get_hybrid_recent_overview(limit: int = 12) -> dict[str, Any]:
    """Resumen agregado reciente para entrenamiento híbrido usando actividades completas."""
    limit = max(1, min(20, int(limit)))

    with FETCH_LOCK:
        api = _get_api()
        activities, err = _optional_call_first(api, ("get_activities",), 0, limit)
        if activities is None:
            raise RuntimeError(err or "No pude leer las actividades recientes")

        bundles: list[dict[str, Any]] = []
        for activity in activities[:limit]:
            if not isinstance(activity, dict):
                continue
            activity_id = activity.get("activityId")
            if activity_id is None:
                continue
            bundles.append(_fetch_activity_bundle(api, str(activity_id), include_time_series=False))

    sessions = [_bundle_hybrid_session(bundle) for bundle in bundles]

    running_like = [x for x in sessions if x.get("activity_type") in {"running", "treadmill_running", "walking", "hiking", "cardio", "elliptical"}]
    strength = [x for x in sessions if x.get("activity_type") == "strength_training"]

    return {
        "overview": _accumulate_type_totals(sessions),
        "sessions": sessions,
        "running_like_sessions": running_like,
        "strength_sessions": strength,
    }

# === MCPX HYBRID OVERVIEW TOOL END ===


# === MCPX HYBRID COACH SNAPSHOT START ===

def _coach_num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _coach_round(value: Any, ndigits: int = 1) -> float | None:
    num = _coach_num(value)
    if num is None:
        return None
    return round(num, ndigits)


def _coach_pick_latest_by_type(sessions: list[dict[str, Any]], activity_type: str) -> dict[str, Any] | None:
    for item in sessions:
        if item.get("activity_type") == activity_type:
            return item
    return None


def _coach_pick_latest_running_like(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    wanted = {"running", "treadmill_running", "walking", "hiking", "cardio", "elliptical"}
    for item in sessions:
        if item.get("activity_type") in wanted:
            return item
    return None


def _coach_first_present(d: dict[str, Any], *keys: str) -> Any:
    if not isinstance(d, dict):
        return None
    for key in keys:
        if d.get(key) is not None:
            return d.get(key)
    return None


def _coach_status_es(value: Any) -> Any:
    mapping = {
        "BALANCED": "Equilibrado",
        "LOW": "Bajo",
        "MODERATE": "Moderada",
        "HIGH": "Alto",
        "OPTIMAL": "Óptimo",
        "MAINTAINING": "Mantenimiento",
        "PRODUCTIVE": "Productivo",
        "RECOVERY": "Recuperación",
        "UNBALANCED": "Desequilibrado",
        "VO2MAX": "VO2 máximo",
        "LACTATE_THRESHOLD": "Umbral de lactato",
        "ANAEROBIC_CAPACITY": "Capacidad anaeróbica",
        "TEMPO": "Tempo",
        "AEROBIC_BASE": "Base aeróbica",
    }
    if value is None:
        return None
    return mapping.get(str(value), value)


def _coach_clean_session_fields(session: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(session, dict):
        return session

    out = dict(session)
    activity_type = out.get("activity_type")

    if activity_type != "strength_training":
        for key in [
            "exercise_set_count",
            "active_sets_estimated",
            "total_reps_estimated",
            "total_volume_kg_estimated",
            "max_weight_kg_seen",
        ]:
            if out.get(key) in (0, 0.0):
                out[key] = None

    if activity_type == "strength_training":
        for key in [
            "average_power",
            "normalized_power",
            "average_run_cadence",
            "ground_contact_time_ms",
            "vertical_oscillation_cm",
            "vertical_ratio",
            "stride_length_cm",
            "stamina_begin",
            "stamina_end",
            "stamina_min",
        ]:
            out[key] = None

    out["training_effect_label_es"] = _coach_status_es(out.get("training_effect_label"))
    return out


def _coach_build_takeaways(overview: dict[str, Any], day_metrics: dict[str, Any], latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> list[str]:
    tips: list[str] = []

    total_load = _coach_num((overview.get("overview") or {}).get("total_training_load"))
    if total_load is not None:
        tips.append(f"Carga reciente total: {round(total_load, 1)}")

    bb = _coach_num(day_metrics.get("body_battery_current"))
    readiness = _coach_num(day_metrics.get("training_readiness"))
    sleep_score = _coach_num(day_metrics.get("sleep_score"))
    hrv_last = _coach_num(day_metrics.get("hrv_last_night"))
    stress_avg = _coach_num(day_metrics.get("stress_avg"))

    if bb is not None:
        tips.append(f"Body Battery actual: {round(bb)}")
    if readiness is not None:
        tips.append(f"Predisposición para entrenar: {round(readiness)}")
    if sleep_score is not None:
        tips.append(f"Puntuación de sueño: {round(sleep_score)}")
    if hrv_last is not None:
        tips.append(f"VFC nocturna: {round(hrv_last)} ms")
    if stress_avg is not None:
        tips.append(f"Estrés medio diario: {round(stress_avg)}")

    if latest_run:
        run_load = latest_run.get("training_load")
        run_te = latest_run.get("training_effect_aerobic")
        run_gct = latest_run.get("ground_contact_time_ms")
        run_vr = latest_run.get("vertical_ratio")
        run_stamina_end = latest_run.get("stamina_end")
        run_type = latest_run.get("activity_type")
        if run_load is not None:
            tips.append(f"Última sesión endurance ({run_type}) carga: {run_load}")
        if run_te is not None:
            tips.append(f"Último TE aeróbico endurance: {run_te}")
        if run_gct is not None:
            tips.append(f"Último GCT endurance: {run_gct} ms")
        if run_vr is not None:
            tips.append(f"Último ratio vertical endurance: {run_vr}")
        if run_stamina_end is not None:
            tips.append(f"Energía disponible final de la última sesión endurance: {run_stamina_end}")

    if latest_strength:
        strength_load = latest_strength.get("training_load")
        reps = latest_strength.get("total_reps_estimated")
        volume = latest_strength.get("total_volume_kg_estimated")
        sets_ = latest_strength.get("active_sets_estimated")
        if strength_load is not None:
            tips.append(f"Última fuerza carga: {strength_load}")
        if sets_ is not None:
            tips.append(f"Última fuerza sets activos: {sets_}")
        if reps is not None:
            tips.append(f"Última fuerza repeticiones estimadas: {reps}")
        if volume is not None:
            tips.append(f"Última fuerza volumen estimado: {volume} kg")

    return tips


@mcp.tool
def get_hybrid_coach_snapshot(limit: int = 12, target_date: str | None = None) -> dict[str, Any]:
    """Resumen híbrido listo para coaching con carga reciente + contexto diario."""
    limit = max(1, min(20, int(limit)))

    if not target_date:
        target_date = date.today().isoformat()

    recent = get_hybrid_recent_overview(limit)
    sessions = recent.get("sessions") or []

    latest_run = _coach_pick_latest_running_like(sessions)
    latest_strength = _coach_pick_latest_by_type(sessions, "strength_training")

    try:
        daily = _collect_day_snapshot(target_date, include_recent_activities=False)
    except Exception as exc:
        daily = {
            "target_date": target_date,
            "metrics": {},
            "raw_sources": {},
            "error": str(exc),
        }

    metrics = daily.get("metrics") or {}

    daily_context = {
        "training_readiness": _coach_first_present(
            metrics,
            "training_readiness",
            "predisposicion_para_entrenar",
        ),
        "training_readiness_label": _coach_first_present(
            metrics,
            "training_readiness_label",
            "predisposicion_para_entrenar_estado",
        ),
        "sleep_score": _coach_first_present(metrics, "sleep_score"),
        "sleep_duration_h": _coach_first_present(metrics, "sleep_duration_h"),
        "hrv_last_night": _coach_first_present(metrics, "hrv_last_night"),
        "hrv_weekly_avg": _coach_first_present(metrics, "hrv_weekly_avg"),
        "body_battery_current": _coach_first_present(metrics, "body_battery_current", "body_battery_actual"),
        "body_battery_max": _coach_first_present(metrics, "body_battery_max"),
        "body_battery_min": _coach_first_present(metrics, "body_battery_min"),
        "resting_hr": _coach_first_present(metrics, "resting_hr"),
        "resting_hr_7d": _coach_first_present(metrics, "resting_hr_7d_avg", "resting_hr_7d"),
        "stress_avg": _coach_first_present(metrics, "stress_avg"),
        "stress_label": _coach_status_es(_coach_first_present(metrics, "stress_label")),
        "active_kcal": _coach_first_present(metrics, "active_kcal", "calorias_activas"),
        "total_kcal": _coach_first_present(metrics, "total_kcal", "calorias_totales"),
        "steps": _coach_first_present(metrics, "steps"),
        "distance_km": _coach_first_present(metrics, "distance_km"),
        "intensity_minutes_weekly": _coach_first_present(
            metrics,
            "intensity_minutes_weekly_total",
            "minutos_intensidad_total_semanal",
        ),
        "training_status": _coach_first_present(metrics, "training_status"),
        "training_status_es": _coach_first_present(metrics, "training_status_es"),
        "vo2max": _coach_first_present(metrics, "vo2max"),
        "vo2max_label": _coach_first_present(metrics, "vo2max_label"),
        "acute_load": _coach_first_present(metrics, "acute_load"),
        "acute_load_status": _coach_first_present(metrics, "acute_load_status"),
        "acute_load_status_es": _coach_first_present(metrics, "acute_load_status_es"),
    }

    result = {
        "target_date": target_date,
        "overview": recent.get("overview"),
        "latest_sessions": {
            "running_like": _coach_clean_session_fields(latest_run),
            "strength": _coach_clean_session_fields(latest_strength),
        },
        "daily_context": daily_context,
        "coach_takeaways": _coach_build_takeaways(
            recent,
            daily_context,
            _coach_clean_session_fields(latest_run),
            _coach_clean_session_fields(latest_strength),
        ),
    }

    return result

# === MCPX HYBRID COACH SNAPSHOT END ===


# === MCPX HYBRID COACH DECISION START ===

def _decision_num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _decision_pick_primary_driver(ctx: dict[str, Any], latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> str:
    readiness = _decision_num(ctx.get("training_readiness"))
    bb = _decision_num(ctx.get("body_battery_current"))
    sleep = _decision_num(ctx.get("sleep_score"))
    hrv = _decision_num(ctx.get("hrv_last_night"))
    acute = _decision_num(ctx.get("acute_load"))

    if readiness is not None and readiness <= 45:
        return "Predisposición para entrenar baja o moderada-baja"
    if bb is not None and bb <= 35:
        return "Body Battery bajo"
    if sleep is not None and sleep <= 65:
        return "Sueño mejorable"
    if latest_run and _decision_num(latest_run.get("training_load")) and _decision_num(latest_run.get("training_load")) >= 220:
        return "La última sesión endurance fue exigente"
    if latest_strength and _decision_num(latest_strength.get("training_load")) and _decision_num(latest_strength.get("training_load")) >= 60:
        return "La última sesión de fuerza dejó carga relevante"
    if acute is not None:
        return "Carga aguda reciente"
    if hrv is not None:
        return "Contexto de VFC reciente"
    return "Contexto general de recuperación"


def _decision_collect_reasons(ctx: dict[str, Any], latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> list[str]:
    reasons: list[str] = []

    readiness = _decision_num(ctx.get("training_readiness"))
    bb = _decision_num(ctx.get("body_battery_current"))
    sleep = _decision_num(ctx.get("sleep_score"))
    hrv = _decision_num(ctx.get("hrv_last_night"))
    stress = _decision_num(ctx.get("stress_avg"))
    acute = _decision_num(ctx.get("acute_load"))
    acute_status_es = ctx.get("acute_load_status_es")
    training_status_es = ctx.get("training_status_es")

    if readiness is not None:
        reasons.append(f"Predisposición para entrenar: {round(readiness)}")
    if bb is not None:
        reasons.append(f"Body Battery actual: {round(bb)}")
    if sleep is not None:
        reasons.append(f"Puntuación de sueño: {round(sleep)}")
    if hrv is not None:
        reasons.append(f"VFC nocturna: {round(hrv)} ms")
    if stress is not None:
        reasons.append(f"Estrés medio: {round(stress)}")
    if acute is not None:
        if acute_status_es:
            reasons.append(f"Carga aguda: {round(acute)} ({acute_status_es})")
        else:
            reasons.append(f"Carga aguda: {round(acute)}")
    if training_status_es:
        reasons.append(f"Estado de entreno: {training_status_es}")

    if latest_run:
        run_load = _decision_num(latest_run.get("training_load"))
        run_te = _decision_num(latest_run.get("training_effect_aerobic"))
        run_stamina_end = _decision_num(latest_run.get("stamina_end"))
        if run_load is not None:
            reasons.append(f"Última sesión endurance carga: {round(run_load, 1)}")
        if run_te is not None:
            reasons.append(f"Último TE aeróbico endurance: {round(run_te, 1)}")
        if run_stamina_end is not None:
            reasons.append(f"Energía disponible final endurance: {round(run_stamina_end)}")

    if latest_strength:
        strength_load = _decision_num(latest_strength.get("training_load"))
        sets_ = _decision_num(latest_strength.get("active_sets_estimated"))
        volume = _decision_num(latest_strength.get("total_volume_kg_estimated"))
        if strength_load is not None:
            reasons.append(f"Última fuerza carga: {round(strength_load, 1)}")
        if sets_ is not None:
            reasons.append(f"Última fuerza sets activos: {round(sets_)}")
        if volume is not None:
            reasons.append(f"Última fuerza volumen estimado: {round(volume)} kg")

    return reasons


def _decision_collect_risks(ctx: dict[str, Any], latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> list[str]:
    risks: list[str] = []

    readiness = _decision_num(ctx.get("training_readiness"))
    bb = _decision_num(ctx.get("body_battery_current"))
    sleep = _decision_num(ctx.get("sleep_score"))
    stress = _decision_num(ctx.get("stress_avg"))

    if readiness is not None and readiness <= 45:
        risks.append("La predisposición para entrenar no es alta.")
    if bb is not None and bb <= 35:
        risks.append("El Body Battery es bajo para meter calidad agresiva.")
    if sleep is not None and sleep <= 65:
        risks.append("El sueño no ha sido especialmente reparador.")
    if stress is not None and stress >= 40:
        risks.append("El estrés medio diario no es bajo.")

    if latest_run:
        gct = _decision_num(latest_run.get("ground_contact_time_ms"))
        vr = _decision_num(latest_run.get("vertical_ratio"))
        run_load = _decision_num(latest_run.get("training_load"))
        if run_load is not None and run_load >= 220:
            risks.append("La última sesión endurance dejó una carga alta.")
        if gct is not None and gct >= 295:
            risks.append("El tiempo de contacto con el suelo reciente es relativamente alto.")
        if vr is not None and vr >= 9.0:
            risks.append("La relación vertical reciente es exigente para sostener más intensidad.")

    if latest_strength:
        sets_ = _decision_num(latest_strength.get("active_sets_estimated"))
        volume = _decision_num(latest_strength.get("total_volume_kg_estimated"))
        if sets_ is not None and sets_ >= 18:
            risks.append("La última sesión de fuerza tuvo bastante volumen de trabajo.")
        if volume is not None and volume >= 10000:
            risks.append("El volumen total de fuerza reciente es alto.")

    return risks


def _decision_level(ctx: dict[str, Any], latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> tuple[str, str, str]:
    readiness = _decision_num(ctx.get("training_readiness"))
    bb = _decision_num(ctx.get("body_battery_current"))
    sleep = _decision_num(ctx.get("sleep_score"))

    latest_run_load = _decision_num((latest_run or {}).get("training_load"))
    latest_strength_load = _decision_num((latest_strength or {}).get("training_load"))

    if (readiness is not None and readiness <= 45) or (bb is not None and bb <= 28) or (sleep is not None and sleep <= 50):
        return (
            "descanso_recuperacion",
            "Descanso o recuperación",
            "Hoy priorizaría recuperación, movilidad o paseo suave."
        )

    if (
        (readiness is not None and readiness <= 60)
        or (bb is not None and bb <= 45)
        or (sleep is not None and sleep <= 65)
        or (latest_run_load is not None and latest_run_load >= 220)
        or (latest_strength_load is not None and latest_strength_load >= 60)
    ):
        return (
            "suave_controlado",
            "Día suave o controlado",
            "Hoy encaja mejor una sesión suave, técnica o trabajo aeróbico controlado."
        )

    return (
        "intensidad_controlada",
        "Intensidad controlada",
        "Hoy podrías meter calidad, pero con control de volumen y sin encadenar fatiga innecesaria."
    )


def _decision_recommendation_text(level_key: str, latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> str:
    if level_key == "descanso_recuperacion":
        return (
            "Haz descanso, movilidad o paseo muy suave de 20-40 min. "
            "Nada de calidad ni fuerza dura."
        )

    if level_key == "suave_controlado":
        if latest_run and latest_strength:
            return (
                "Haz endurance suave en Z2 real 30-45 min o una fuerza ligera/técnica recortando volumen. "
                "Evita combinar fuerza pesada con trabajo intenso de carrera."
            )
        return (
            "Haz una sesión suave y controlada, priorizando técnica, base aeróbica o fuerza ligera."
        )

    return (
        "Puedes hacer una sesión de calidad controlada. "
        "Mejor una sola pieza principal: tempo/umbral en cinta o carrera, o fuerza principal con volumen contenido."
    )


@mcp.tool
def get_hybrid_coach_decision(limit: int = 12, target_date: str | None = None) -> dict[str, Any]:
    """Devuelve una decisión diaria lista para entrenador a partir del snapshot híbrido."""
    snap = get_hybrid_coach_snapshot(limit=limit, target_date=target_date)

    ctx = snap.get("daily_context") or {}
    latest = snap.get("latest_sessions") or {}
    latest_run = latest.get("running_like")
    latest_strength = latest.get("strength")

    level_key, level_title, summary_text = _decision_level(ctx, latest_run, latest_strength)

    result = {
        "target_date": snap.get("target_date"),
        "decision": {
            "level_key": level_key,
            "level_title": level_title,
            "summary": summary_text,
            "primary_driver": _decision_pick_primary_driver(ctx, latest_run, latest_strength),
            "recommended_action": _decision_recommendation_text(level_key, latest_run, latest_strength),
        },
        "reasons": _decision_collect_reasons(ctx, latest_run, latest_strength),
        "risks": _decision_collect_risks(ctx, latest_run, latest_strength),
        "daily_context": ctx,
        "latest_sessions": latest,
        "overview": snap.get("overview"),
    }

    return result

# === MCPX HYBRID COACH DECISION END ===


# === MCPX HYBRID USER BRIEFING START ===

def _brief_num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _brief_int(value: Any) -> int | None:
    num = _brief_num(value)
    if num is None:
        return None
    return int(round(num))


def _brief_primary_message(decision: dict[str, Any], ctx: dict[str, Any]) -> str:
    title = decision.get("level_title") or "Día sin clasificar"
    readiness = _brief_int(ctx.get("training_readiness"))
    bb = _brief_int(ctx.get("body_battery_current"))
    sleep = _brief_int(ctx.get("sleep_score"))

    parts = [title]
    if readiness is not None:
        parts.append(f"predisposición {readiness}")
    if bb is not None:
        parts.append(f"Body Battery {bb}")
    if sleep is not None:
        parts.append(f"sueño {sleep}")
    return " · ".join(parts)


def _brief_plan(decision: dict[str, Any], ctx: dict[str, Any], latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> dict[str, Any]:
    level_key = decision.get("level_key")
    acute = _brief_int(ctx.get("acute_load"))
    acute_es = ctx.get("acute_load_status_es")
    latest_run_load = _brief_num((latest_run or {}).get("training_load"))
    latest_strength_load = _brief_num((latest_strength or {}).get("training_load"))

    if level_key == "descanso_recuperacion":
        return {
            "tipo": "recuperación",
            "objetivo": "bajar fatiga y facilitar recuperación",
            "duracion_recomendada_min": "20-40",
            "intensidad": "muy suave",
            "sesion_sugerida": "movilidad, paseo suave o descanso completo",
            "detalle": [
                "Nada de series ni fuerza dura.",
                "Si haces algo, que sea fácil de cortar y sin perseguir métricas.",
                "Prioriza llegar fresco a mañana."
            ],
            "contexto_carga": f"Carga aguda {acute} ({acute_es})" if acute is not None and acute_es else acute,
        }

    if level_key == "suave_controlado":
        sesion = "endurance suave en Z2 real 30-45 min"
        if latest_run_load is not None and latest_run_load >= 220:
            sesion = "rodaje muy controlado o cinta suave 30-40 min"
        elif latest_strength_load is not None and latest_strength_load >= 60:
            sesion = "fuerza técnica ligera o endurance suave sin meter calidad"

        return {
            "tipo": "suave_controlado",
            "objetivo": "sumar trabajo útil sin añadir fatiga innecesaria",
            "duracion_recomendada_min": "30-45",
            "intensidad": "suave / controlada",
            "sesion_sugerida": sesion,
            "detalle": [
                "Mantén margen respiratorio claro.",
                "No conviertas una sesión suave en una sesión media.",
                "Mejor una sola pieza principal y terminar con sensación de reserva."
            ],
            "contexto_carga": f"Carga aguda {acute} ({acute_es})" if acute is not None and acute_es else acute,
        }

    return {
        "tipo": "intensidad_controlada",
        "objetivo": "aprovechar el día sin desbordar la recuperación",
        "duracion_recomendada_min": "35-60",
        "intensidad": "calidad controlada",
        "sesion_sugerida": "tempo/umbral controlado o fuerza principal con volumen contenido",
        "detalle": [
            "Haz una sola parte exigente.",
            "Controla el volumen más que la intensidad pico.",
            "No mezcles fuerza pesada con una carrera dura el mismo día."
        ],
        "contexto_carga": f"Carga aguda {acute} ({acute_es})" if acute is not None and acute_es else acute,
    }


def _brief_avoid_list(decision: dict[str, Any], latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> list[str]:
    level_key = decision.get("level_key")
    out: list[str] = []

    if level_key == "descanso_recuperacion":
        out.extend([
            "series intensas",
            "fuerza pesada",
            "doble sesión",
        ])
    elif level_key == "suave_controlado":
        out.extend([
            "encadenar fuerza pesada con carrera intensa",
            "rodaje que se te vaya a Z4-Z5",
            "más volumen del previsto por sensaciones de inicio",
        ])
    else:
        out.extend([
            "hacer dos sesiones duras en el mismo día",
            "alargar volumen por encima de lo planificado",
        ])

    run_load = _brief_num((latest_run or {}).get("training_load"))
    if run_load is not None and run_load >= 220:
        out.append("repetir otra sesión endurance muy exigente demasiado pronto")

    strength_vol = _brief_num((latest_strength or {}).get("total_volume_kg_estimated"))
    if strength_vol is not None and strength_vol >= 10000:
        out.append("meter otra fuerza de mucho volumen sin recorte")

    dedup = []
    seen = set()
    for item in out:
        if item not in seen:
            seen.add(item)
            dedup.append(item)
    return dedup


def _brief_nutrition_recovery(decision: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    level_key = decision.get("level_key")
    bb = _brief_int(ctx.get("body_battery_current"))
    sleep = _brief_int(ctx.get("sleep_score"))
    active_kcal = _brief_int(ctx.get("active_kcal"))

    if level_key == "descanso_recuperacion":
        return {
            "pre": "Comida normal y estable, sin necesidad de cargar hidratos de forma agresiva.",
            "post": "Prioriza proteína suficiente y una comida completa si haces movilidad o paseo.",
            "hidratacion": "Bebe de forma regular durante el día; no hace falta estrategia agresiva.",
            "sueno": "Prioridad alta a dormir antes y mejor.",
            "nota": f"Body Battery {bb}" if bb is not None else None,
        }

    if level_key == "suave_controlado":
        return {
            "pre": "Llega con algo de energía disponible; evita entrenar vacío si la sesión cae en una franja larga.",
            "post": "Proteína + hidrato moderado tras la sesión para recuperar sin castigar el sueño.",
            "hidratacion": "Rehidrata de forma simple y constante; si sudas mucho, mete sodio.",
            "sueno": "Hoy el sueño es un objetivo del plan, no un detalle.",
            "nota": f"Sueño {sleep} / kcal activas {active_kcal}" if sleep is not None or active_kcal is not None else None,
        }

    return {
        "pre": "Llega alimentado y sin ayunos largos si vas a meter calidad.",
        "post": "Proteína + hidratos dentro de la primera hora si la sesión ha sido seria.",
        "hidratacion": "Rehidrata y repón sales si la sesión es larga o calurosa.",
        "sueno": "Protege el sueño para consolidar la carga.",
        "nota": f"Body Battery {bb} · sueño {sleep}" if bb is not None or sleep is not None else None,
    }


@mcp.tool
def get_hybrid_user_briefing(limit: int = 12, target_date: str | None = None) -> dict[str, Any]:
    """Resumen listo para usuario final: qué toca hoy, por qué, qué evitar y cómo recuperar."""
    decision_pack = get_hybrid_coach_decision(limit=limit, target_date=target_date)

    decision = decision_pack.get("decision") or {}
    ctx = decision_pack.get("daily_context") or {}
    latest = decision_pack.get("latest_sessions") or {}
    latest_run = latest.get("running_like")
    latest_strength = latest.get("strength")

    return {
        "target_date": decision_pack.get("target_date"),
        "mensaje_principal": _brief_primary_message(decision, ctx),
        "que_toca_hoy": _brief_plan(decision, ctx, latest_run, latest_strength),
        "por_que": decision_pack.get("reasons") or [],
        "riesgos_a_vigilar": decision_pack.get("risks") or [],
        "evitar_hoy": _brief_avoid_list(decision, latest_run, latest_strength),
        "nutricion_y_recuperacion": _brief_nutrition_recovery(decision, ctx),
        "driver_principal": decision.get("primary_driver"),
        "accion_recomendada": decision.get("recommended_action"),
        "resumen_decision": decision.get("summary"),
    }

# === MCPX HYBRID USER BRIEFING END ===


# === MCPX HYBRID NUTRITION BRIEFING START ===

def _nutrition_num(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _nutrition_int(value: Any) -> int | None:
    num = _nutrition_num(value)
    if num is None:
        return None
    return int(round(num))


def _nutrition_reference_for_activity(activity_id: Any) -> dict[str, Any] | None:
    if not activity_id:
        return None
    try:
        full = get_activity_full(str(activity_id), include_time_series=False)
    except Exception:
        return None

    summary = full.get("summary") or {}
    return {
        "activity_id": str(activity_id),
        "water_estimated_ml": _nutrition_int(summary.get("waterEstimated")),
        "calories_total": _nutrition_int(summary.get("calories")),
        "duration_min": round(float(summary.get("duration") or 0) / 60.0, 1) if summary.get("duration") is not None else None,
        "training_load": _nutrition_num(summary.get("activityTrainingLoad")),
        "activity_type": full.get("activity_type"),
        "activity_name": full.get("activity_name"),
    }


def _nutrition_focus(decision: dict[str, Any], ctx: dict[str, Any], latest_strength: dict[str, Any] | None) -> str:
    level_key = decision.get("level_key")
    bb = _nutrition_int(ctx.get("body_battery_current"))
    sleep = _nutrition_int(ctx.get("sleep_score"))
    strength_volume = _nutrition_num((latest_strength or {}).get("total_volume_kg_estimated"))

    if level_key == "descanso_recuperacion":
        return "recuperación y sueño"
    if strength_volume is not None and strength_volume >= 10000:
        return "recuperación muscular + recarga moderada"
    if (bb is not None and bb <= 40) or (sleep is not None and sleep <= 65):
        return "disponibilidad energética estable y recuperación"
    return "energía útil para entrenar sin pasarte"


def _nutrition_pre_training(decision: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    level_key = decision.get("level_key")
    bb = _nutrition_int(ctx.get("body_battery_current"))
    sleep = _nutrition_int(ctx.get("sleep_score"))

    if level_key == "descanso_recuperacion":
        return {
            "objetivo": "llegar estable al día sin buscar una carga agresiva",
            "recomendacion": "Haz una comida normal y completa. No hace falta estrategia específica preentreno si solo haces movilidad o paseo.",
            "ejemplos": [
                "yogur o queso fresco + fruta",
                "tostadas con jamón, pavo o huevos",
                "comida normal con arroz/patata y proteína"
            ],
        }

    if level_key == "suave_controlado":
        extra = "Evita entrenar completamente vacío." if (bb is not None and bb <= 40) or (sleep is not None and sleep <= 65) else "No necesitas una carga alta de hidratos."
        return {
            "objetivo": "tener energía disponible sin pesadez",
            "recomendacion": "Si entrenas tras muchas horas sin comer, mete 20-40 g de hidratos y algo fácil de digerir 30-90 min antes.",
            "ejemplos": [
                "plátano + yogur",
                "tostada con miel o mermelada",
                "fruta + batido o vaso de leche"
            ],
            "nota": extra,
        }

    return {
        "objetivo": "llegar con glucógeno disponible para calidad",
        "recomendacion": "Mete 30-60 g de hidratos 1-3 h antes y evita ayunos largos si la sesión va a ser exigente.",
        "ejemplos": [
            "arroz o avena + proteína ligera",
            "pan o tostadas + fruta",
            "yogur + cereales + fruta"
        ],
    }


def _nutrition_post_training(decision: dict[str, Any], latest_run_ref: dict[str, Any] | None, latest_strength_ref: dict[str, Any] | None) -> dict[str, Any]:
    level_key = decision.get("level_key")

    if level_key == "descanso_recuperacion":
        return {
            "objetivo": "recuperar sin sobrecompensar",
            "recomendacion": "Con una comida completa rica en proteína y vegetales suele bastar si el trabajo es muy suave.",
            "ejemplos": [
                "tortilla o pollo con patata/arroz",
                "yogur alto en proteína + fruta",
                "legumbre + proteína + verduras"
            ],
        }

    if level_key == "suave_controlado":
        note = None
        if latest_run_ref and latest_run_ref.get("water_estimated_ml"):
            note = f"Tu última sesión endurance estimó {latest_run_ref['water_estimated_ml']} ml de pérdida."
        return {
            "objetivo": "recuperar sin castigar el sueño ni meter déficit",
            "recomendacion": "Después entrena con 25-35 g de proteína y 30-60 g de hidratos si la sesión finalmente tiene algo de volumen.",
            "ejemplos": [
                "batido o yogur alto en proteína + fruta + cereales",
                "arroz/patata/pan + pollo, atún o huevos",
                "queso fresco batido + fruta + avena"
            ],
            "nota": note,
        }

    note = None
    if latest_strength_ref and latest_strength_ref.get("calories_total"):
        note = f"La última fuerza gastó ~{latest_strength_ref['calories_total']} kcal totales."
    return {
        "objetivo": "reponer glucógeno y facilitar recuperación",
        "recomendacion": "Después de calidad prioriza 25-40 g de proteína y 60-90 g de hidratos dentro de la primera hora o en la comida siguiente.",
        "ejemplos": [
            "arroz/pasta/patata + proteína magra",
            "batido + fruta + pan o cereales",
            "comida completa con hidrato principal y proteína suficiente"
        ],
        "nota": note,
    }


def _nutrition_hydration(decision: dict[str, Any], latest_run_ref: dict[str, Any] | None, latest_strength_ref: dict[str, Any] | None) -> dict[str, Any]:
    level_key = decision.get("level_key")

    refs = []
    for item in (latest_run_ref, latest_strength_ref):
        if isinstance(item, dict) and item.get("water_estimated_ml") is not None:
            refs.append(int(item["water_estimated_ml"]))

    ref_text = None
    if refs:
        ref_text = f"Pérdidas recientes estimadas: {max(refs)} ml en una sesión."

    if level_key == "descanso_recuperacion":
        return {
            "recomendacion": "Hidrátate de forma estable durante el día. No hace falta estrategia agresiva.",
            "sodio": "Útil si sudas mucho o vienes de días de calor.",
            "referencia": ref_text,
        }

    if level_key == "suave_controlado":
        return {
            "recomendacion": "Suma agua de forma constante antes y después. Si sudas bastante, añade sodio o una bebida con sales.",
            "sodio": "Moderado-alto si la sesión se alarga o hace calor.",
            "referencia": ref_text,
        }

    return {
        "recomendacion": "Llega bien hidratado y repón agua + sodio después, especialmente si hay calor o sudor alto.",
        "sodio": "Recomendable si la sesión es exigente o larga.",
        "referencia": ref_text,
    }


def _nutrition_avoid_today(decision: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    level_key = decision.get("level_key")
    bb = _nutrition_int(ctx.get("body_battery_current"))
    sleep = _nutrition_int(ctx.get("sleep_score"))

    out = []

    if level_key in {"descanso_recuperacion", "suave_controlado"}:
        out.append("entrenar en ayunas si llegas vacío o con poca energía")
        out.append("recortar demasiado hidratos y luego pedirle calidad al cuerpo")

    out.append("hacer una sesión mejor de lo planificado y no comer después")
    out.append("dejar la hidratación para el final del día")

    if bb is not None and bb <= 40:
        out.append("acumular déficit energético con Body Battery bajo")
    if sleep is not None and sleep <= 65:
        out.append("cenar pobre en proteína o hidratos tras un día ya tocado por el sueño")

    dedup = []
    seen = set()
    for item in out:
        if item not in seen:
            seen.add(item)
            dedup.append(item)
    return dedup


def _nutrition_reasoning(ctx: dict[str, Any], latest_run: dict[str, Any] | None, latest_strength: dict[str, Any] | None) -> list[str]:
    out = []

    readiness = _nutrition_int(ctx.get("training_readiness"))
    bb = _nutrition_int(ctx.get("body_battery_current"))
    sleep = _nutrition_int(ctx.get("sleep_score"))
    active_kcal = _nutrition_int(ctx.get("active_kcal"))
    weekly_intensity = _nutrition_int(ctx.get("intensity_minutes_weekly"))

    if readiness is not None:
        out.append(f"Predisposición para entrenar {readiness}")
    if bb is not None:
        out.append(f"Body Battery {bb}")
    if sleep is not None:
        out.append(f"Sueño {sleep}")
    if active_kcal is not None:
        out.append(f"Calorías activas del día {active_kcal}")
    if weekly_intensity is not None:
        out.append(f"Minutos de intensidad semanales {weekly_intensity}")

    if latest_run and latest_run.get("training_load") is not None:
        out.append(f"Última sesión endurance carga {latest_run['training_load']}")
    if latest_strength and latest_strength.get("total_volume_kg_estimated") is not None:
        out.append(f"Última fuerza volumen {int(round(float(latest_strength['total_volume_kg_estimated'])))} kg")

    return out


@mcp.tool
def get_hybrid_nutrition_briefing(limit: int = 12, target_date: str | None = None) -> dict[str, Any]:
    """Plan nutricional diario práctico usando la decisión híbrida y el contexto reciente."""
    decision_pack = get_hybrid_coach_decision(limit=limit, target_date=target_date)

    decision = decision_pack.get("decision") or {}
    ctx = decision_pack.get("daily_context") or {}
    latest = decision_pack.get("latest_sessions") or {}
    latest_run = latest.get("running_like")
    latest_strength = latest.get("strength")

    latest_run_ref = _nutrition_reference_for_activity((latest_run or {}).get("activity_id"))
    latest_strength_ref = _nutrition_reference_for_activity((latest_strength or {}).get("activity_id"))

    return {
        "target_date": decision_pack.get("target_date"),
        "foco_nutricional": _nutrition_focus(decision, ctx, latest_strength),
        "antes_de_entrenar": _nutrition_pre_training(decision, ctx),
        "despues_de_entrenar": _nutrition_post_training(decision, latest_run_ref, latest_strength_ref),
        "hidratacion": _nutrition_hydration(decision, latest_run_ref, latest_strength_ref),
        "evitar_hoy": _nutrition_avoid_today(decision, ctx),
        "por_que": _nutrition_reasoning(ctx, latest_run, latest_strength),
        "decision_base": {
            "driver_principal": decision.get("primary_driver"),
            "accion_recomendada": decision.get("recommended_action"),
            "resumen_decision": decision.get("summary"),
        }
    }

# === MCPX HYBRID NUTRITION BRIEFING END ===


# === HISTORICAL DATA TOOLS START ===

_HISTORY_MAX_ACTIVITIES_PER_PAGE = 100
_HISTORY_MAX_WELLNESS_DAYS = 30
_HISTORY_SLEEP_BETWEEN_DAYS_S = 0.35


def _compact_activity_for_history(activity: dict) -> dict:
    """Normalización compacta optimizada para listas históricas largas."""
    activity_type = (
        activity.get("activityType") or activity.get("activityTypeDTO") or {}
    )
    type_key = activity_type.get("typeKey")
    summary = activity.get("summaryDTO") or {}

    duration_s = activity.get("duration") or summary.get("duration")
    distance_m = activity.get("distance") or summary.get("distance")

    return {
        "activity_id": activity.get("activityId"),
        "name": activity.get("activityName"),
        "type": type_key,
        "type_es": _ACTIVITY_TYPE_ES.get(type_key, type_key) if type_key else None,
        "activity_family": _activity_family(type_key),
        "start_time_local": (
            activity.get("startTimeLocal") or summary.get("startTimeLocal")
        ),
        "duration_min": round(float(duration_s) / 60, 1) if duration_s is not None else None,
        "distance_km": round(float(distance_m) / 1000, 2) if distance_m is not None else None,
        "avg_hr": activity.get("averageHR") or summary.get("averageHR"),
        "max_hr": activity.get("maxHR") or summary.get("maxHR"),
        "calories": activity.get("calories") or summary.get("calories"),
        "training_load": (
            activity.get("activityTrainingLoad")
            or activity.get("trainingLoad")
            or summary.get("activityTrainingLoad")
        ),
        "elevation_gain_m": activity.get("elevationGain") or summary.get("elevationGain"),
        "training_effect": summary.get("trainingEffect"),
    }


@mcp.tool
def get_activities_paged(limit: int = 100, offset: int = 0) -> dict:
    """Acceso paginado a todo el historial de actividades Garmin.
    limit máximo 100. Usa offset en múltiplos de limit para navegar el historial completo.
    Ejemplo: offset=0 primeras 100, offset=100 siguientes 100, etc.
    has_more=true indica que hay más actividades disponibles.
    """
    limit = max(1, min(_HISTORY_MAX_ACTIVITIES_PER_PAGE, int(limit)))
    offset = max(0, int(offset))

    with FETCH_LOCK:
        api = _get_api()
        activities, err = _optional_call_first(api, ("get_activities",), offset, limit)

    if activities is None:
        raise RuntimeError(err or "No pude leer el historial de actividades")

    if not isinstance(activities, list):
        activities = []

    normalized = [
        _compact_activity_for_history(a)
        for a in activities
        if isinstance(a, dict)
    ]

    return {
        "offset": offset,
        "limit": limit,
        "count": len(normalized),
        "has_more": len(normalized) == limit,
        "next_offset": offset + len(normalized),
        "activities": normalized,
    }


@mcp.tool
def get_activities_in_range(
    start_date: str,
    end_date: str = None,
    activity_type: str = None,
) -> dict:
    """Actividades entre dos fechas (formato YYYY-MM-DD).
    activity_type es opcional: running, strength_training, cycling, etc.
    Sin end_date usa hoy. Lista ordenada de más reciente a más antigua.
    Para rangos muy amplios (más de 1 año) prefiere get_activities_paged con paginación.
    """
    start = _parse_date(start_date)
    end = _parse_date(end_date) if end_date else _today_local().isoformat()

    if start > end:
        start, end = end, start

    with FETCH_LOCK:
        api = _get_api()
        if activity_type:
            activities, err = _optional_call_first(
                api, ("get_activities_by_date",), start, end, activity_type
            )
        else:
            activities, err = _optional_call_first(
                api, ("get_activities_by_date",), start, end
            )

    if activities is None:
        raise RuntimeError(err or f"No pude leer actividades entre {start} y {end}")

    if not isinstance(activities, list):
        activities = []

    normalized = [
        _compact_activity_for_history(a)
        for a in activities
        if isinstance(a, dict)
    ]
    normalized.sort(key=lambda x: x.get("start_time_local") or "", reverse=True)

    return {
        "start_date": start,
        "end_date": end,
        "activity_type_filter": activity_type,
        "count": len(normalized),
        "activities": normalized,
    }


def _compact_wellness_for_range(api, target_date: str) -> dict:
    """Snapshot wellness ligero para una fecha.
    No usa FETCH_LOCK ni llama a _get_api() — debe invocarse con api ya obtenido.
    """
    summary, _ = _optional_call_first(
        api, ("get_user_summary", "get_stats"), target_date
    )
    sleep, _ = _optional_call_first(api, ("get_sleep_data",), target_date)
    hrv, _ = _optional_call_first(api, ("get_hrv_data",), target_date)

    sm = summary or {}
    sleep_dto = ((sleep or {}).get("dailySleepDTO")) or {}
    hrv_summary = ((hrv or {}).get("hrvSummary")) or {}

    sleep_score = None
    try:
        sleep_score = sleep_dto["sleepScores"]["overall"]["value"]
    except Exception:
        pass

    sleep_seconds = sleep_dto.get("sleepTimeSeconds")
    distance_m = sm.get("totalDistanceMeters")

    return {
        "date": target_date,
        "steps": sm.get("totalSteps"),
        "distance_km": round(float(distance_m) / 1000, 2) if distance_m is not None else None,
        "active_kcal": sm.get("activeKilocalories"),
        "total_kcal": sm.get("totalKilocalories"),
        "resting_hr": _resting_hr(summary),
        "stress_avg": sm.get("averageStressLevel"),
        "stress_label": sm.get("stressQualifier"),
        "body_battery_high": sm.get("bodyBatteryHighestValue"),
        "body_battery_low": sm.get("bodyBatteryLowestValue"),
        "body_battery_end": sm.get("bodyBatteryMostRecentValue"),
        "sleep_score": sleep_score,
        "sleep_hours": round(float(sleep_seconds) / 3600, 1) if sleep_seconds is not None else None,
        "hrv_last_night": hrv_summary.get("lastNightAvg"),
        "hrv_status": hrv_summary.get("status"),
    }


@mcp.tool
def get_daily_wellness(target_date: str) -> dict:
    """Obtiene métricas completas de un día específico.
    Incluye: pasos, distancia, calorías, FC en reposo, estrés, Body Battery, VFC y más.
    Formato de fecha: YYYY-MM-DD (ejemplo: 2017-06-15)
    """
    parsed_date = _parse_date(target_date)
    return _collect_day_snapshot(parsed_date, include_recent_activities=False)


@mcp.tool
def get_wellness_range(
    start_date: str,
    end_date: str = None,
) -> dict:
    """Resumen wellness diario compacto para un rango de fechas.
    Incluye pasos, distancia, calorías, FC en reposo, estrés, Body Battery, sueño y VFC.
    Máximo 30 días por llamada. Para periodos mayores llama varias veces desplazando start_date.
    Ejemplo 3 meses: llamada 1 start=2025-01-01 end=2025-01-30,
                     llamada 2 start=2025-01-31 end=2025-03-01, etc.
    """
    start_dt = date.fromisoformat(_parse_date(start_date))
    end_dt = date.fromisoformat(
        _parse_date(end_date) if end_date else _today_local().isoformat()
    )

    if start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    total_days = (end_dt - start_dt).days + 1
    clipped = total_days > _HISTORY_MAX_WELLNESS_DAYS
    if clipped:
        end_dt = start_dt + timedelta(days=_HISTORY_MAX_WELLNESS_DAYS - 1)
        total_days = _HISTORY_MAX_WELLNESS_DAYS

    with FETCH_LOCK:
        api = _get_api()

    days_data = []
    errors = []

    for i in range(total_days):
        target = (start_dt + timedelta(days=i)).isoformat()
        try:
            with FETCH_LOCK:
                day = _compact_wellness_for_range(api, target)
            days_data.append(day)
        except Exception as exc:
            errors.append({"date": target, "error": str(exc)})
        if i < total_days - 1:
            time.sleep(_HISTORY_SLEEP_BETWEEN_DAYS_S)

    return {
        "start_date": start_dt.isoformat(),
        "end_date": end_dt.isoformat(),
        "days_requested": total_days,
        "days_returned": len(days_data),
        "clipped_to_max": clipped,
        "max_days_per_call": _HISTORY_MAX_WELLNESS_DAYS,
        "note": (
            f"Rango recortado a {_HISTORY_MAX_WELLNESS_DAYS} dias. "
            f"Llama de nuevo con start_date={end_dt.isoformat()} para continuar."
        ) if clipped else None,
        "days": days_data,
        "errors": errors if errors else None,
    }


@mcp.tool
def get_race_predictions(
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Predicciones de tiempo de carrera de Garmin para 5K, 10K, media maratón y maratón.
    Sin fechas devuelve las predicciones actuales.
    Formato fechas: YYYY-MM-DD.
    """
    sd = _parse_date(start_date) if start_date else None
    ed = _parse_date(end_date) if end_date else None

    with FETCH_LOCK:
        api = _get_api()
        if sd and ed:
            data, err = _optional_call_first(api, ("get_race_predictions",), sd, ed)
        else:
            data, err = _optional_call_first(api, ("get_race_predictions",))

    if data is None:
        raise RuntimeError(err or "No se pudieron obtener predicciones de carrera")

    return {"race_predictions": data, "start_date": sd, "end_date": ed}


@mcp.tool
def get_personal_records() -> dict:
    """Récords personales del usuario por distancia y tipo de actividad.
    Incluye mejores tiempos en carrera, ciclismo y otros deportes registrados en Garmin.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_personal_record", "get_personal_records"))

    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los récords personales")

    return {"personal_records": data}


@mcp.tool
def get_fitness_age(target_date: str = None) -> dict:
    """Edad física (Fitness Age) calculada por Garmin.
    Compara tu condición física con tu edad cronológica.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)

    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(
            api, ("get_fitnessage_data", "get_fitness_age"), parsed
        )

    if data is None:
        raise RuntimeError(err or "No se pudo obtener la edad física")

    return {"fitness_age_data": data, "date": parsed}


@mcp.tool
def get_endurance_score(
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Puntuación de resistencia aeróbica (Endurance Score) de Garmin.
    Rango de fechas para ver la evolución. Sin fechas usa los últimos 28 días.
    Formato: YYYY-MM-DD.
    """
    ed = _parse_date(end_date) if end_date else _today_local().isoformat()
    sd = _parse_date(start_date) if start_date else (
        date.fromisoformat(ed) - timedelta(days=27)
    ).isoformat()

    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_endurance_score",), sd, ed)

    if data is None:
        raise RuntimeError(err or "No se pudo obtener el Endurance Score")

    return {"endurance_score": data, "start_date": sd, "end_date": ed}


@mcp.tool
def get_hill_score(
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Puntuación de rendimiento en montaña/desnivel (Hill Score) de Garmin.
    Evalúa tu capacidad en subidas. Sin fechas usa los últimos 28 días.
    Formato: YYYY-MM-DD.
    """
    ed = _parse_date(end_date) if end_date else _today_local().isoformat()
    sd = _parse_date(start_date) if start_date else (
        date.fromisoformat(ed) - timedelta(days=27)
    ).isoformat()

    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_hill_score",), sd, ed)

    if data is None:
        raise RuntimeError(err or "No se pudo obtener el Hill Score")

    return {"hill_score": data, "start_date": sd, "end_date": ed}


@mcp.tool
def get_goals(status: str = "active") -> dict:
    """Objetivos de entrenamiento del usuario en Garmin Connect.
    status: 'active' (activos), 'future' (futuros) o 'past' (pasados).
    """
    if status not in ("active", "future", "past"):
        status = "active"

    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_goals",), status, 1, 50)

    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los objetivos")

    return {"goals": data, "status_filter": status}


@mcp.tool
def get_gear(include_stats: bool = True) -> dict:
    """Material deportivo registrado en Garmin (zapatillas, bicicletas, etc.) con kilometraje y estadísticas.
    include_stats=True añade actividades y distancias acumuladas por cada pieza de material.
    """
    with FETCH_LOCK:
        api = _get_api()
        profile, _ = _optional_call_first(api, ("get_user_profile",))
        profile_number = None
        if isinstance(profile, dict):
            profile_number = (
                (profile.get("userData") or {}).get("profileNumber")
                or (profile.get("userData") or {}).get("id")
                or profile.get("profileNumber")
                or profile.get("id")
            )

        if profile_number is None:
            raise RuntimeError("No se pudo obtener el número de perfil de usuario")

        gear_list, err = _optional_call_first(api, ("get_gear",), profile_number)

        if gear_list is None:
            raise RuntimeError(err or "No se pudo obtener el material deportivo")

        if include_stats and isinstance(gear_list, list):
            for item in gear_list:
                uuid = item.get("uuid") or item.get("gearPk")
                if uuid:
                    stats, _ = _optional_call_first(api, ("get_gear_stats",), uuid)
                    if stats is not None:
                        item["stats"] = stats

    return {"gear": gear_list, "profile_number": profile_number}


@mcp.tool
def get_activity_evaluation(activity_id: str) -> dict:
    """Evaluación de entrenador virtual de Garmin para una actividad específica.
    Incluye retroalimentación sobre el rendimiento, esfuerzo y consejos de recuperación.
    activity_id: identificador numérico de la actividad.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_activity_evaluation",), activity_id)

    if data is None:
        raise RuntimeError(err or f"No se pudo obtener la evaluación de la actividad {activity_id}")

    return {"activity_id": activity_id, "evaluation": data}


@mcp.tool
def get_weigh_ins(
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Historial de pesajes registrados en Garmin Connect.
    Sin fechas devuelve los últimos 30 días. Formato: YYYY-MM-DD.
    """
    ed = _parse_date(end_date) if end_date else _today_local().isoformat()
    sd = _parse_date(start_date) if start_date else (
        date.fromisoformat(ed) - timedelta(days=29)
    ).isoformat()

    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_weigh_ins",), sd, ed)

    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los pesajes")

    return {"weigh_ins": data, "start_date": sd, "end_date": ed}


@mcp.tool
def add_weigh_in(
    weight_kg: float,
    target_date: str = None,
) -> dict:
    """Registra un nuevo pesaje en Garmin Connect.
    weight_kg: peso en kilogramos (puede ser decimal, ej: 75.5).
    target_date: fecha en formato YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date) if target_date else _today_local().isoformat()
    weight_int = round(weight_kg * 1000)

    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(
            api, ("add_weigh_in",), weight_int, "kg", parsed
        )

    if data is None:
        raise RuntimeError(err or "No se pudo registrar el pesaje")

    return {"ok": True, "weight_kg": weight_kg, "date": parsed, "response": data}


# === EXTRA API TOOLS START ===

@mcp.tool
def get_activity_splits(activity_id: str) -> dict:
    """Splits kilométricos/por milla detallados de una actividad.
    Incluye ritmo, FC, distancia y tiempo por cada split.
    activity_id: identificador numérico de la actividad.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_activity_splits",), activity_id)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los splits de {activity_id}")
    return {"activity_id": activity_id, "splits": data}


@mcp.tool
def get_activity_split_summaries(activity_id: str) -> dict:
    """Resumen de splits de una actividad (por fase o segmento).
    Complementa get_activity_splits con totales por bloque.
    activity_id: identificador numérico de la actividad.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_activity_split_summaries",), activity_id)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los resúmenes de splits de {activity_id}")
    return {"activity_id": activity_id, "split_summaries": data}


@mcp.tool
def get_activity_hr_in_timezones(activity_id: str) -> dict:
    """Distribución del tiempo por zona de frecuencia cardíaca en una actividad.
    Muestra cuánto tiempo se pasó en cada zona Z1-Z5.
    activity_id: identificador numérico de la actividad.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_activity_hr_in_timezones",), activity_id)
    if data is None:
        raise RuntimeError(err or f"No se pudo obtener la distribución de FC por zonas de {activity_id}")
    return {"activity_id": activity_id, "hr_in_timezones": data}


@mcp.tool
def get_activity_exercise_sets(activity_id: str) -> dict:
    """Series de ejercicios de un entrenamiento de fuerza.
    Incluye nombre del ejercicio, series, repeticiones, peso y duración.
    activity_id: identificador numérico de la actividad.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_activity_exercise_sets",), activity_id)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los ejercicios de {activity_id}")
    return {"activity_id": activity_id, "exercise_sets": data}


@mcp.tool
def get_activity_weather(activity_id: str) -> dict:
    """Condiciones meteorológicas durante una actividad.
    Incluye temperatura, humedad, viento y condición general.
    activity_id: identificador numérico de la actividad.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_activity_weather",), activity_id)
    if data is None:
        raise RuntimeError(err or f"No se pudo obtener el tiempo meteorológico de {activity_id}")
    return {"activity_id": activity_id, "weather": data}


@mcp.tool
def get_activity_gear(activity_id: str) -> dict:
    """Material deportivo utilizado en una actividad concreta.
    Útil para saber qué zapatillas o bicicleta se usó en cada entreno.
    activity_id: identificador numérico de la actividad.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_activity_gear",), activity_id)
    if data is None:
        raise RuntimeError(err or f"No se pudo obtener el material de {activity_id}")
    return {"activity_id": activity_id, "gear": data}


@mcp.tool
def get_last_activity() -> dict:
    """Última actividad registrada en Garmin Connect.
    Acceso rápido sin necesidad de conocer el activity_id.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_last_activity",))
    if data is None:
        raise RuntimeError(err or "No se pudo obtener la última actividad")
    return {"last_activity": data}


@mcp.tool
def get_activity_types() -> dict:
    """Lista de todos los tipos de actividad disponibles en Garmin Connect.
    Útil para conocer los valores válidos del filtro activity_type.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_activity_types",))
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los tipos de actividad")
    return {"activity_types": data}


@mcp.tool
def get_all_day_stress(target_date: str = None) -> dict:
    """Curva de estrés minuto a minuto durante todo el día.
    Permite ver picos y valles de estrés a lo largo del día.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_all_day_stress",), parsed)
    if data is None:
        raise RuntimeError(err or f"No se pudo obtener el estrés del día {parsed}")
    return {"date": parsed, "all_day_stress": data}


@mcp.tool
def get_steps_data(target_date: str = None) -> dict:
    """Serie temporal de pasos a lo largo del día (intervalos de 15 min).
    Permite ver la distribución de actividad durante el día.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_steps_data",), parsed)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los pasos de {parsed}")
    return {"date": parsed, "steps_data": data}


@mcp.tool
def get_daily_steps(
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Pasos diarios totales en un rango de fechas.
    Sin fechas usa los últimos 7 días. Formato: YYYY-MM-DD.
    """
    ed = _parse_date(end_date) if end_date else _today_local().isoformat()
    sd = _parse_date(start_date) if start_date else (
        date.fromisoformat(ed) - timedelta(days=6)
    ).isoformat()
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_daily_steps",), sd, ed)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los pasos diarios entre {sd} y {ed}")
    return {"start_date": sd, "end_date": ed, "daily_steps": data}


@mcp.tool
def get_floors(target_date: str = None) -> dict:
    """Pisos subidos y bajados durante el día.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_floors", "get_floors_data"), parsed)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los pisos de {parsed}")
    return {"date": parsed, "floors": data}


@mcp.tool
def get_blood_pressure(
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Registros de presión arterial en un rango de fechas.
    Solo disponible si el dispositivo o la app registra tensión arterial.
    Sin fechas usa los últimos 7 días. Formato: YYYY-MM-DD.
    """
    ed = _parse_date(end_date) if end_date else _today_local().isoformat()
    sd = _parse_date(start_date) if start_date else (
        date.fromisoformat(ed) - timedelta(days=6)
    ).isoformat()
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(
            api, ("get_blood_pressure", "get_blood_pressure_data"), sd, ed
        )
    if data is None:
        raise RuntimeError(err or "No se pudo obtener la presión arterial")
    return {"start_date": sd, "end_date": ed, "blood_pressure": data}


@mcp.tool
def get_stats_and_body(target_date: str = None) -> dict:
    """Resumen combinado de actividad diaria y composición corporal.
    Combina pasos, calorías, distancia y peso en una sola llamada.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_stats_and_body",), parsed)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener stats+cuerpo de {parsed}")
    return {"date": parsed, "stats_and_body": data}


@mcp.tool
def get_progress_summary(
    start_date: str = None,
    end_date: str = None,
    metric: str = "distance",
) -> dict:
    """Progresión de una métrica entre dos fechas.
    metric: 'distance' (distancia), 'duration' (tiempo), 'elevationGain' (desnivel),
            'movingDuration', 'calories', 'bmrCalories', 'steps'.
    Sin fechas usa los últimos 30 días. Formato: YYYY-MM-DD.
    """
    ed = _parse_date(end_date) if end_date else _today_local().isoformat()
    sd = _parse_date(start_date) if start_date else (
        date.fromisoformat(ed) - timedelta(days=29)
    ).isoformat()
    valid = {"distance","duration","elevationGain","movingDuration","calories","bmrCalories","steps"}
    if metric not in valid:
        metric = "distance"
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(
            api, ("get_progress_summary_between_dates",), sd, ed, metric
        )
    if data is None:
        raise RuntimeError(err or f"No se pudo obtener el progreso de {metric}")
    return {"start_date": sd, "end_date": ed, "metric": metric, "progress": data}


@mcp.tool
def get_earned_badges() -> dict:
    """Insignias y logros conseguidos en Garmin Connect.
    Muestra todos los badges desbloqueados hasta la fecha.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_earned_badges",))
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener las insignias")
    return {"earned_badges": data}


@mcp.tool
def get_badge_challenges(start: int = 1, limit: int = 20) -> dict:
    """Retos de insignias activos en Garmin Connect.
    start: índice inicial (paginación). limit: máximo de resultados.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_badge_challenges",), start, limit)
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los retos de insignias")
    return {"start": start, "limit": limit, "badge_challenges": data}


@mcp.tool
def get_adhoc_challenges(start: int = 1, limit: int = 20) -> dict:
    """Retos espontáneos activos en Garmin Connect.
    start: índice inicial (paginación). limit: máximo de resultados.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_adhoc_challenges",), start, limit)
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los retos espontáneos")
    return {"start": start, "limit": limit, "adhoc_challenges": data}


@mcp.tool
def get_available_badge_challenges(start: int = 1, limit: int = 20) -> dict:
    """Retos de insignias disponibles para unirse en Garmin Connect.
    start: índice inicial (paginación). limit: máximo de resultados.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_available_badge_challenges",), start, limit)
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los retos disponibles")
    return {"start": start, "limit": limit, "available_challenges": data}


@mcp.tool
def get_device_last_used() -> dict:
    """Información del último dispositivo Garmin utilizado para sincronizar.
    Incluye modelo, firmware y fecha de última conexión.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_device_last_used",))
    if data is None:
        raise RuntimeError(err or "No se pudo obtener el último dispositivo usado")
    return {"device_last_used": data}


@mcp.tool
def get_gear_stats(gear_uuid: str) -> dict:
    """Estadísticas de uso de una pieza de material concreto (zapatillas, bicicleta…).
    Devuelve actividades totales, distancia acumulada y tiempo de uso.
    gear_uuid: identificador UUID del material (obtenible con get_gear).
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_gear_stats",), gear_uuid)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener las estadísticas del material {gear_uuid}")
    return {"gear_uuid": gear_uuid, "stats": data}


@mcp.tool
def get_gear_defaults() -> dict:
    """Material por defecto asignado a cada tipo de actividad (correr, ciclismo, etc.).
    Útil para saber qué zapatilla o bici tiene Garmin asignada por defecto en cada deporte.
    """
    with FETCH_LOCK:
        api = _get_api()
        profile, _ = _optional_call_first(api, ("get_user_profile",))
        profile_number = None
        if isinstance(profile, dict):
            profile_number = (
                (profile.get("userData") or {}).get("profileNumber")
                or (profile.get("userData") or {}).get("id")
                or profile.get("profileNumber")
                or profile.get("id")
            )
        if profile_number is None:
            raise RuntimeError("No se pudo obtener el número de perfil de usuario")
        data, err = _optional_call_first(api, ("get_gear_defaults",), profile_number)
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los materiales por defecto")
    return {"profile_number": profile_number, "gear_defaults": data}


@mcp.tool
def get_daily_weigh_ins(target_date: str = None) -> dict:
    """Todos los pesajes registrados en un día concreto.
    Útil cuando hay varios registros en el mismo día.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_daily_weigh_ins",), parsed)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los pesajes del día {parsed}")
    return {"date": parsed, "daily_weigh_ins": data}


@mcp.tool
def get_inprogress_virtual_challenges(start: int = 1, limit: int = 20) -> dict:
    """Retos virtuales en curso en Garmin Connect (por ejemplo Garmin Challenges de km).
    start: índice inicial (paginación). limit: máximo de resultados.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_inprogress_virtual_challenges",), start, limit)
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los retos virtuales en curso")
    return {"start": start, "limit": limit, "inprogress_virtual_challenges": data}


@mcp.tool
def get_non_completed_badge_challenges(start: int = 1, limit: int = 20) -> dict:
    """Retos de insignias que aún no se han completado.
    Complementa get_badge_challenges mostrando los pendientes.
    start: índice inicial (paginación). limit: máximo de resultados.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_non_completed_badge_challenges",), start, limit)
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los retos de insignias pendientes")
    return {"start": start, "limit": limit, "non_completed_badge_challenges": data}


@mcp.tool
def get_device_alarms() -> dict:
    """Alarmas configuradas en los dispositivos Garmin vinculados a la cuenta.
    Devuelve las alarmas activas y sus configuraciones.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_device_alarms",))
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener las alarmas del dispositivo")
    return {"device_alarms": data}


@mcp.tool
def get_user_profile_info() -> dict:
    """Información básica del perfil de usuario: nombre completo y sistema de unidades.
    Útil para saber si Garmin trabaja en km/kg o millas/libras.
    """
    with FETCH_LOCK:
        api = _get_api()
        full_name, name_err = _optional_call_first(api, ("get_full_name",))
        unit_system, unit_err = _optional_call_first(api, ("get_unit_system",))
    return {
        "full_name": full_name,
        "unit_system": unit_system,
        "errors": {k: v for k, v in {"name": name_err, "units": unit_err}.items() if v},
    }


@mcp.tool
def delete_weigh_in(weight_pk: str, target_date: str) -> dict:
    """Elimina un pesaje concreto por su clave primaria.
    weight_pk: identificador del pesaje (campo weightPk de get_weigh_ins o get_daily_weigh_ins).
    target_date: fecha del pesaje en formato YYYY-MM-DD.
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("delete_weigh_in",), weight_pk, parsed)
    if data is None and err:
        raise RuntimeError(err)
    return {"ok": True, "weight_pk": weight_pk, "date": parsed, "response": data}


@mcp.tool
def delete_weigh_ins(target_date: str, delete_all: bool = False) -> dict:
    """Elimina los pesajes de una fecha concreta.
    delete_all=True elimina todos los registros del día; False elimina solo el más reciente.
    target_date: fecha en formato YYYY-MM-DD.
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("delete_weigh_ins",), parsed, delete_all)
    if data is None and err:
        raise RuntimeError(err)
    return {"ok": True, "date": parsed, "delete_all": delete_all, "response": data}


@mcp.tool
def set_gear_default(activity_type: str, gear_uuid: str, is_default: bool = True) -> dict:
    """Asigna (o desasigna) una pieza de material como predeterminada para un tipo de actividad.
    activity_type: tipo de actividad Garmin (p.ej. 'running', 'cycling').
    gear_uuid: UUID del material (obtenible con get_gear).
    is_default: True para asignar como predeterminado, False para quitar esa asignación.
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("set_gear_default",), activity_type, gear_uuid, is_default)
    if data is None and err:
        raise RuntimeError(err)
    return {"ok": True, "activity_type": activity_type, "gear_uuid": gear_uuid, "is_default": is_default, "response": data}


@mcp.tool
def get_spo2_data(target_date: str = None) -> dict:
    """Datos de oximetría de pulso (SpO2) del día.
    Muestra el nivel de saturación de oxígeno en sangre registrado por el sensor del reloj.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_spo2_data", "get_pulse_ox_data"), parsed)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los datos de SpO2 del día {parsed}")
    return {"date": parsed, "spo2": data}


@mcp.tool
def get_respiration_data(target_date: str = None) -> dict:
    """Frecuencia respiratoria registrada durante el día y el sueño.
    Útil para detectar tendencias de recuperación y estado de forma aeróbica.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_respiration_data",), parsed)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los datos de respiración del día {parsed}")
    return {"date": parsed, "respiration": data}


@mcp.tool
def get_hydration_data(target_date: str = None) -> dict:
    """Registro de hidratación del día (vasos de agua u oz registrados manualmente).
    Muestra el objetivo diario y el progreso hasta ese momento.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_hydration_data",), parsed)
    if data is None:
        raise RuntimeError(err or f"No se pudieron obtener los datos de hidratación del día {parsed}")
    return {"date": parsed, "hydration": data}


@mcp.tool
def get_body_composition(
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """Composición corporal en un rango de fechas: peso, IMC y porcentaje de grasa.
    Sin fechas devuelve los últimos 30 días. Formato: YYYY-MM-DD.
    """
    ed = _parse_date(end_date) if end_date else _today_local().isoformat()
    sd = _parse_date(start_date) if start_date else (
        date.fromisoformat(ed) - timedelta(days=29)
    ).isoformat()
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(
            api, ("get_body_composition", "get_weight_data"), sd, ed
        )
    if data is None:
        raise RuntimeError(err or "No se pudo obtener la composición corporal")
    return {"start_date": sd, "end_date": ed, "body_composition": data}


# === WORKOUT & CALENDAR TOOLS ===

@mcp.tool
def get_scheduled_workouts(year: int = None, month: int = None) -> dict:
    """Entrenamientos planificados en el calendario de Garmin Connect para un mes.
    Por defecto usa el mes actual. month: 1-12 (enero=1).
    Incluye el deporte, nombre, fecha y ID para obtener el detalle completo.
    """
    today = _today_local()
    y = int(year) if year else today.year
    m = int(month) if month else today.month
    # La API de Garmin usa mes 0-indexado (enero=0)
    with FETCH_LOCK:
        api = _get_api()
        try:
            # Intentar con el método de la librería si existe, si no usar connectapi
            if hasattr(api, "get_workouts_calendar"):
                data = api.get_workouts_calendar(y, m)
            else:
                data = api.connectapi(f"/calendar-service/year/{y}/month/{m - 1}")
        except Exception as e:
            raise RuntimeError(f"No se pudieron obtener los entrenamientos planificados de {y}/{m:02d}: {e}")
    # Filtrar solo elementos tipo workout del calendario
    items = data if isinstance(data, list) else (data.get("calendarItems") or data.get("items") or [data] if isinstance(data, dict) else [])
    workouts = [i for i in items if isinstance(i, dict) and i.get("itemType", "").lower() in ("workout", "garmincoach", "")]
    return {
        "year": y,
        "month": m,
        "calendar_raw": data,
        "workouts_this_month": workouts,
        "total_items": len(items),
    }


@mcp.tool
def get_todays_workout() -> dict:
    """Entrenamiento planificado para hoy en Garmin Connect.
    Acceso rápido sin necesidad de especificar fecha.
    """
    today = _today_local()
    with FETCH_LOCK:
        api = _get_api()
        try:
            if hasattr(api, "get_workouts_calendar"):
                data = api.get_workouts_calendar(today.year, today.month)
            else:
                data = api.connectapi(f"/calendar-service/year/{today.year}/month/{today.month - 1}")
        except Exception as e:
            raise RuntimeError(f"No se pudo obtener el calendario de hoy: {e}")
    items = data if isinstance(data, list) else (data.get("calendarItems") or data.get("items") or [])
    today_iso = today.isoformat()
    todays = [i for i in items if isinstance(i, dict) and str(i.get("date", "")).startswith(today_iso)]
    return {
        "date": today_iso,
        "todays_items": todays,
        "has_workout": any(i.get("itemType", "").lower() in ("workout", "garmincoach") for i in todays),
        "calendar_raw": data,
    }


@mcp.tool
def get_workout_library(start: int = 0, limit: int = 20) -> dict:
    """Biblioteca de entrenamientos guardados en Garmin Connect.
    Devuelve nombre, deporte e ID de cada entrenamiento guardado.
    start: offset para paginación. limit: máximo de resultados (máx 100).
    """
    limit = max(1, min(100, int(limit)))
    with FETCH_LOCK:
        api = _get_api()
        try:
            if hasattr(api, "get_workouts"):
                data = api.get_workouts(start, limit)
            else:
                data = api.connectapi(f"/workout-service/workouts?start={start}&limit={limit}&myWorkoutsOnly=true&sharedWorkoutsOnly=false")
        except Exception as e:
            raise RuntimeError(f"No se pudo obtener la biblioteca de entrenamientos: {e}")
    return {"start": start, "limit": limit, "workouts": data}


@mcp.tool
def get_workout_detail(workout_id: str) -> dict:
    """Detalle completo de un entrenamiento: pasos, series, zonas de FC/potencia y objetivos.
    workout_id: ID numérico del entrenamiento (obtenible con get_workout_library o get_scheduled_workouts).
    """
    with FETCH_LOCK:
        api = _get_api()
        try:
            if hasattr(api, "get_workout_by_id"):
                data = api.get_workout_by_id(workout_id)
            else:
                data = api.connectapi(f"/workout-service/workout/{workout_id}")
        except Exception as e:
            raise RuntimeError(f"No se pudo obtener el detalle del entrenamiento {workout_id}: {e}")
    return {"workout_id": workout_id, "workout": data}


@mcp.tool
def get_training_plans() -> dict:
    """Planes de entrenamiento activos en Garmin Connect.
    Incluye el nombre del plan, deporte, fase actual y fechas.
    """
    with FETCH_LOCK:
        api = _get_api()
        try:
            if hasattr(api, "get_training_plans"):
                data = api.get_training_plans()
            else:
                data = api.connectapi("/trainingplan-service/trainingplan/plans")
        except Exception as e:
            raise RuntimeError(f"No se pudieron obtener los planes de entrenamiento: {e}")
    return {"training_plans": data}


@mcp.tool
def get_training_plan_detail(plan_id: str) -> dict:
    """Detalle completo de un plan de entrenamiento: fases, semanas y workouts programados.
    plan_id: ID del plan (obtenible con get_training_plans).
    """
    with FETCH_LOCK:
        api = _get_api()
        data = None
        for path in (f"/trainingplan-service/trainingplan/phased/{plan_id}",
                     f"/trainingplan-service/trainingplan/fbt-adaptive/{plan_id}"):
            try:
                data = api.connectapi(path)
                if data:
                    break
            except Exception:
                continue
    if data is None:
        raise RuntimeError(f"No se pudo obtener el detalle del plan {plan_id}")
    return {"plan_id": plan_id, "training_plan": data}


@mcp.tool
def schedule_workout(workout_id: str, target_date: str) -> dict:
    """Planifica un entrenamiento de la biblioteca en una fecha concreta.
    workout_id: ID del entrenamiento a programar.
    target_date: fecha en formato YYYY-MM-DD.
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        try:
            if hasattr(api, "schedule_workout"):
                data = api.schedule_workout(workout_id, parsed)
            else:
                data = api.garth.post("connectapi", f"/workout-service/schedule/{workout_id}", json={"date": parsed})
        except Exception as e:
            raise RuntimeError(f"No se pudo planificar el entrenamiento {workout_id} para {parsed}: {e}")
    return {"ok": True, "workout_id": workout_id, "date": parsed, "response": data}


@mcp.tool
def unschedule_workout(schedule_id: str) -> dict:
    """Elimina un entrenamiento de la planificación por su schedule ID.
    schedule_id: ID de la entrada en el calendario (campo scheduledWorkoutId en get_scheduled_workouts).
    """
    with FETCH_LOCK:
        api = _get_api()
        try:
            if hasattr(api, "unschedule_workout"):
                data = api.unschedule_workout(schedule_id)
            else:
                data = api.garth.delete("connectapi", f"/workout-service/schedule/{schedule_id}")
        except Exception as e:
            raise RuntimeError(f"No se pudo eliminar el workout planificado {schedule_id}: {e}")
    return {"ok": True, "schedule_id": schedule_id, "response": data}


@mcp.tool
def get_nutrition_log(target_date: str = None) -> dict:
    """Registro de alimentación del día: alimentos, macronutrientes y calorías.
    Requiere que el usuario registre alimentos en Garmin Connect o la app.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        food_log, meals, settings = None, None, None
        for method, path in (
            ("food_log",    f"/nutrition-service/food/logs/{parsed}"),
            ("meals",       f"/nutrition-service/meals/{parsed}"),
            ("settings",    f"/nutrition-service/settings/{parsed}"),
        ):
            try:
                result = api.connectapi(path)
                if method == "food_log":
                    food_log = result
                elif method == "meals":
                    meals = result
                else:
                    settings = result
            except Exception:
                pass
    if food_log is None and meals is None:
        raise RuntimeError(f"No hay datos de nutrición para {parsed}. Asegúrate de registrar alimentos en Garmin Connect.")
    return {
        "date": parsed,
        "food_log": food_log,
        "meals": meals,
        "settings": settings,
    }

# === WORKOUT & CALENDAR TOOLS END ===

# === EXTRA API TOOLS END ===

# === HISTORICAL DATA TOOLS END ===

# === NEW GARMIN API TOOLS START ===

@mcp.tool
def upload_activity(file_base64: str, name: str = "", description: str = "", activity_type: str = "") -> dict:
    """Sube un fichero .fit a Garmin Connect.
    file_base64: contenido del fichero codificado en base64.
    name: nombre de la actividad (opcional).
    description: descripción (opcional).
    activity_type: tipo de actividad, p.ej. 'running', 'cycling' (opcional).
    """
    import base64 as _b64, tempfile, os
    try:
        data = _b64.b64decode(file_base64)
    except Exception as e:
        raise ValueError(f"file_base64 no es base64 válido: {e}")
    with FETCH_LOCK:
        api = _get_api()
        with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as f:
            f.write(data)
            tmp_path = f.name
        try:
            result = api.upload_activity(tmp_path)
        except Exception as e:
            raise RuntimeError(f"Error subiendo actividad: {e}")
        finally:
            os.unlink(tmp_path)
    return {"ok": True, "result": result, "name": name or None}


@mcp.tool
def download_activity(activity_id: str, format: str = "gpx") -> dict:
    """Descarga una actividad en el formato indicado y devuelve el contenido en base64.
    format: 'gpx' (por defecto), 'tcx', 'kml', 'csv', 'fit' / 'original'.
    """
    import base64 as _b64
    fmt_map = {
        "gpx": "GPX",
        "tcx": "TCX",
        "kml": "KML",
        "csv": "CSV",
        "fit": "ORIGINAL",
        "original": "ORIGINAL",
    }
    fmt_key = fmt_map.get(format.lower())
    if fmt_key is None:
        raise ValueError(f"Formato '{format}' no soportado. Usa: gpx, tcx, kml, csv, fit.")
    fmt_enum = getattr(Garmin.ActivityDownloadFormat, fmt_key, None)
    if fmt_enum is None:
        raise ValueError(f"ActivityDownloadFormat.{fmt_key} no existe en esta versión de garminconnect.")
    with FETCH_LOCK:
        api = _get_api()
        try:
            data = api.download_activity(int(activity_id), dl_fmt=fmt_enum)
        except Exception as e:
            raise RuntimeError(f"Error descargando actividad {activity_id} en formato {format}: {e}")
    return {
        "activity_id": activity_id,
        "format": format,
        "content_base64": _b64.b64encode(data).decode(),
        "size_bytes": len(data),
    }


@mcp.tool
def get_devices() -> dict:
    """Lista todos los dispositivos Garmin registrados en la cuenta (relojes, sensores, etc.)."""
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_devices",))
    if data is None:
        raise RuntimeError(err or "No se pudieron obtener los dispositivos.")
    return {"devices": data}


@mcp.tool
def get_device_settings(device_id: str) -> dict:
    """Configuración detallada de un dispositivo Garmin.
    device_id: identificador del dispositivo (obtenible con get_devices).
    """
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_device_settings",), device_id)
    if data is None:
        raise RuntimeError(err or f"No se pudo obtener configuración del dispositivo {device_id}.")
    return {"device_id": device_id, "settings": data}


@mcp.tool
def get_max_metrics(target_date: str = None) -> dict:
    """Métricas máximas: VO2max, umbral de lactato, capacidad anaeróbica, potencia máxima.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_max_metrics",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay métricas máximas para {parsed}.")
    return {"date": parsed, "max_metrics": data}


@mcp.tool
def get_body_battery(target_date: str = None) -> dict:
    """Serie temporal del Body Battery a lo largo de un día concreto.
    Devuelve los valores horarios, no solo el resumen.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_body_battery",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay datos de Body Battery para {parsed}.")
    return {"date": parsed, "body_battery": data}


@mcp.tool
def get_heart_rates(target_date: str = None) -> dict:
    """Serie temporal de frecuencia cardíaca durante un día (lecturas cada ~2 min).
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_heart_rates",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay datos de FC para {parsed}.")
    return {"date": parsed, "heart_rates": data}


@mcp.tool
def get_sleep_data(target_date: str = None) -> dict:
    """Datos completos de sueño: fases, duración, puntuación, respiración nocturna, SpO2.
    Formato fecha: YYYY-MM-DD de la noche (por defecto ayer).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_sleep_data",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay datos de sueño para {parsed}.")
    return {"date": parsed, "sleep_data": data}


@mcp.tool
def get_hrv_data(target_date: str = None) -> dict:
    """Datos detallados de VFC (variabilidad de FC): valores nocturnos, estado, baseline, últimas 5 noches.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_hrv_data",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay datos de VFC para {parsed}.")
    return {"date": parsed, "hrv_data": data}


@mcp.tool
def get_training_status(target_date: str = None) -> dict:
    """Estado de entrenamiento detallado: carga aguda, carga crónica, estado actual y VO2max.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_training_status",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay estado de entrenamiento para {parsed}.")
    return {"date": parsed, "training_status": data}


@mcp.tool
def get_training_readiness(target_date: str = None) -> dict:
    """Predisposición para entrenar detallada: puntuación, factores, recomendación de carga.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_training_readiness",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay datos de predisposición para {parsed}.")
    return {"date": parsed, "training_readiness": data}


@mcp.tool
def get_rhr_day(target_date: str = None) -> dict:
    """FC en reposo medida un día concreto.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_rhr_day",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay FC en reposo para {parsed}.")
    return {"date": parsed, "rhr": data}


@mcp.tool
def get_stress_data(target_date: str = None) -> dict:
    """Serie temporal del nivel de estrés durante el día (0-100, lectura cada ~3 min).
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_stress_data",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay datos de estrés para {parsed}.")
    return {"date": parsed, "stress_data": data}


@mcp.tool
def get_user_summary(target_date: str = None) -> dict:
    """Resumen diario del usuario: pasos, calorías, distancia total, minutos intensidad, pisos.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_user_summary",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay resumen de usuario para {parsed}.")
    return {"date": parsed, "user_summary": data}


@mcp.tool
def get_stats(target_date: str = None) -> dict:
    """Estadísticas diarias crudas: pasos, calorías activas y en reposo, minutos intensidad, pisos.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    with FETCH_LOCK:
        api = _get_api()
        data, err = _optional_call_first(api, ("get_stats",), parsed)
    if data is None:
        raise RuntimeError(err or f"No hay estadísticas para {parsed}.")
    return {"date": parsed, "stats": data}

# === NEW GARMIN API TOOLS END ===

# === MULTI-USER & COACHING TOOLS ===

@mcp.tool
def list_users() -> dict[str, Any]:
    """Lista todos los usuarios registrados. Solo el admin puede usar esta función."""
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}
    db = _load_users_db()
    users_list = []
    for uid, u in db.get("users", {}).items():
        users_list.append({
            "id": uid,
            "display_name": u.get("display_name", ""),
            "garmin_email": u.get("garmin_email", ""),
            "created_at": u.get("created_at", ""),
        })
    return {"users": users_list, "total": len(users_list)}


@mcp.tool
def create_user(display_name: str, garmin_email: str = "") -> dict[str, Any]:
    """Crea un nuevo usuario con su API key. Devuelve la API key para que el usuario la guarde."""
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}
    new_user = _create_user(display_name, garmin_email)
    return {
        "ok": True,
        "user_id": new_user["id"],
        "api_key": new_user["api_key"],
        "display_name": new_user["display_name"],
        "message": "Guarda esta API key, es la única vez que se muestra completa.",
    }


@mcp.tool
def delete_user(user_id: str) -> dict[str, Any]:
    """Elimina un usuario y todos sus datos."""
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}
    ok = _delete_user(user_id)
    return {"ok": ok, "deleted": user_id}


@mcp.tool
def get_user_profile() -> dict[str, Any]:
    """Devuelve el perfil del usuario actual."""
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}
    return {
        "id": user.get("id"),
        "display_name": user.get("display_name", ""),
        "garmin_email": user.get("garmin_email", ""),
        "home_lat": user.get("home_lat"),
        "home_lon": user.get("home_lon"),
        "home_name": user.get("home_name", ""),
    }


@mcp.tool
def update_user_profile(
    display_name: str | None = None,
    garmin_email: str | None = None,
    home_lat: float | None = None,
    home_lon: float | None = None,
    home_name: str | None = None,
) -> dict[str, Any]:
    """Actualiza el perfil del usuario actual. Solo actualiza los campos que se proporcionen."""
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}
    fields = {}
    if display_name is not None:
        fields["display_name"] = display_name
    if garmin_email is not None:
        fields["garmin_email"] = garmin_email
    if home_lat is not None:
        fields["home_lat"] = home_lat
    if home_lon is not None:
        fields["home_lon"] = home_lon
    if home_name is not None:
        fields["home_name"] = home_name
    updated = _update_user(user["id"], **fields)
    return {"ok": updated is not None, "profile": updated}


# === TRAINING PLAN IMPORT TOOLS ===

def _garmin_workout_step_from_desc(step: dict[str, Any], step_order: int) -> dict[str, Any]:
    """Convierte un paso normalizado al formato JSON de workout de Garmin."""
    step_type = step.get("type", "active").lower()
    intensity_map = {
        "warmup": "warmup", "calentamiento": "warmup",
        "active": "active", "interval": "interval", "intervalo": "interval",
        "rest": "rest", "recovery": "recovery", "recuperación": "recovery", "recuperacion": "recovery",
        "cooldown": "cooldown", "vuelta a la calma": "cooldown",
    }
    intensity = intensity_map.get(step_type, "active")

    duration_type = "time"
    duration_value = 0
    if "duration_sec" in step and step["duration_sec"]:
        duration_type = "time"
        duration_value = int(step["duration_sec"])
    elif "duration_min" in step and step["duration_min"]:
        duration_type = "time"
        duration_value = int(step["duration_min"]) * 60
    elif "distance_m" in step and step["distance_m"]:
        duration_type = "distance"
        duration_value = int(step["distance_m"])
    elif "distance_km" in step and step["distance_km"]:
        duration_type = "distance"
        duration_value = int(float(step["distance_km"]) * 1000)

    target_type = "open"
    target_value = 0
    if "target_hr_zone" in step and step["target_hr_zone"]:
        target_type = "heart_rate"
        target_value = int(step["target_hr_zone"])
    elif "target_pace_mps" in step and step["target_pace_mps"]:
        target_type = "speed"
        target_value = float(step["target_pace_mps"])

    result: dict[str, Any] = {
        "stepName": step.get("name", step_type.capitalize()),
        "stepType": {"typeId": {"warmup": 3, "cooldown": 4, "rest": 1, "recovery": 5, "interval": 6, "active": 0}.get(intensity, 0), "typeKey": intensity},
        "durationType": {"typeId": {"time": 0, "distance": 1}.get(duration_type, 0), "typeKey": duration_type},
        "durationValue": duration_value,
    }
    if target_type != "open":
        result["targetType"] = {"typeId": {"heart_rate": 1, "speed": 0}.get(target_type, 2), "typeKey": target_type}
        result["targetValue"] = target_value
        if target_type == "heart_rate":
            result["targetHRZone"] = target_value
    if step_order is not None:
        result["stepOrder"] = step_order
    return result


@mcp.tool
def extract_pdf_text(
    pdf_base64: str,
    filename: str = "plan.pdf",
) -> dict[str, Any]:
    """
    Extrae el texto de un documento PDF (por ejemplo, un plan de entrenamiento
    en PDF) y lo devuelve para que puedas leerlo e interpretarlo.

    pdf_base64: contenido del PDF codificado en base64 (del archivo adjunto).
    filename: nombre opcional del archivo (solo informativo).

    Cuándo usarla: cuando el usuario ha adjuntado un PDF con su plan y necesitas
    leer qué sesiones contiene para después prepararlas con
    create_training_plan / import_natural_language_plan / create_workout_from_description.
    """
    try:
        import base64 as _b64
        import pypdf
    except ImportError:
        return {"error": "La librería pypdf no está instalada en el servidor."}

    try:
        raw = _b64.b64decode(pdf_base64, validate=True)
    except Exception as exc:
        return {"error": f"El base64 del PDF no es válido: {exc}"}

    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
    except Exception as exc:
        return {"error": f"No se pudo leer el PDF: {exc}"}

    pages = []
    full_text = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append({"page": i + 1, "text": text.strip()})
        full_text.append(text)

    return {
        "ok": True,
        "filename": filename,
        "pages": len(reader.pages),
        "char_count": sum(len(p) for p in full_text),
        "pages_text": pages,
        "full_text": "\n\n".join(full_text).strip(),
        "advice": "Interpreta el plan y usa create_training_plan, import_natural_language_plan o create_workout_from_description para cargarlo en Garmin.",
    }


@mcp.tool
def parse_training_pdf(
    pdf_base64: str,
    start_date: str,
    plan_name: str = "Plan de entrenamiento",
    auto_push_to_device: bool = False,
) -> dict[str, Any]:
    """
    Lee un plan de entrenamiento en PDF, lo interpreta y lo crea en Garmin Connect
    con cada sesión estructurada en etapas (calentamiento, series, repeticiones,
    descanso y vuelta a la calma).

    pdf_base64: contenido del PDF codificado en base64.
    start_date: fecha de inicio del plan (YYYY-MM-DD).
    plan_name: nombre del plan.
    auto_push_to_device: si True, envía cada entrenamiento al reloj Garmin.

    El plan debe describir semanas y días. Ejemplo de lo que se interpreta:
      "Semana 1: Lunes 5km suave Z2; Miércoles 3x800m con 400m rec; Viernes 8km
       Semana 2: ..."

    Devuelve un resumen estructurado de las sesiones detectadas y su resultado en Garmin.
    """
    try:
        result = _import_plan_from_pdf_internal(
            pdf_base64=pdf_base64,
            start_date=start_date,
            plan_name=plan_name,
            auto_push_to_device=auto_push_to_device,
        )
        return result
    except Exception as exc:
        return {"error": f"Error procesando el PDF: {exc}"}


def _import_plan_from_pdf_internal(
    pdf_base64: str,
    start_date: str,
    plan_name: str,
    auto_push_to_device: bool,
) -> dict[str, Any]:
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    r = extract_pdf_text(pdf_base64, filename=plan_name + ".pdf")
    if not r.get("ok"):
        return r

    plan_text = r.get("full_text", "")
    if not plan_text.strip():
        return {"error": "El PDF no contiene texto extraíble. Puede ser un PDF escaneado (imagen) sin capa de texto."}

    try:
        base_date = date.fromisoformat(start_date)
    except ValueError:
        return {"error": f"Fecha inválida: {start_date}"}

    sessions = _parse_plan_text_to_sessions(plan_text)
    if not sessions:
        return {"error": "No pude detectar sesiones en el PDF. El formato esperado es 'Semana X: Día descripción'."}

    overview = [
        {
            "session": i + 1,
            "target_date": (base_date + timedelta(days=s["day_offset"])).isoformat(),
            "day_of_week": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo")[s["day_offset"] % 7],
            "description": s["description"],
        }
        for i, s in enumerate(sessions)
    ]

    result = create_training_plan(
        plan_name=plan_name,
        start_date=start_date,
        sessions=sessions,
        auto_push_to_device=auto_push_to_device,
    )

    result["source"] = "pdf"
    result["session_overview"] = overview
    result["notes"] = "Cada sesión se ha creado con sus etapas (calentamiento, series, repeticiones, descanso y vuelta a la calma) a partir de la descripción."
    return result


def _parse_plan_text_to_sessions(plan_text: str) -> list[dict[str, Any]]:
    """Convierte el texto de un plan en una lista de sesiones, una por día.

    Cada línea (o segmento con día) es UNA sesión. Las partes separadas por coma
    dentro de esa sesión son sus ETAPAS (calentamiento, series, repeticiones,
    descanso, vuelta a la calma) y se conservan juntas en 'description' para que
    create_training_plan las desglose correctamente.
    """
    sessions: list[dict[str, Any]] = []
    lines = [l.strip() for l in plan_text.strip().splitlines() if l.strip()]

    day_map = {
        "lunes": 0, "mon": 0, "martes": 1, "tue": 1, "miércoles": 2,
        "miercoles": 2, "wed": 2, "jueves": 3, "thu": 3, "viernes": 4,
        "fri": 4, "sábado": 5, "sabado": 5, "sat": 5, "domingo": 6, "sun": 6,
    }
    day_names = "|".join(day_map.keys())
    day_re = re.compile(rf'\b({day_names})\b\s*[:\-]?\s*', re.I)

    current_week = 0
    seen: dict[int, str] = {}

    def push(day_offset: int, text: str) -> None:
        text = re.sub(r'\s+', ' ', text).strip(' .,;:-')
        if not text:
            return
        # Merge múltiples fragmentos del mismo día en una única sesión.
        if day_offset in seen:
            seen[day_offset] += " " + text
        else:
            seen[day_offset] = text

    for line in lines:
        week_match = re.match(r'(?:semana|week)\s*(\d+)', line, re.I)
        if week_match:
            current_week = int(week_match.group(1)) - 1

        # Descarta líneas que son solo cabeceras (título del plan).
        if not week_match and not day_re.search(line):
            continue

        # Buscar todos los días que aparezcan en la línea y asignar el texto que
        # sigue a cada uno. Si la línea empieza por un día, todo lo posterior
        # (incluidas las partes con coma) pertenece a esa sesión.
        if week_match:
            rest = line[week_match.end():]
            day_match = day_re.search(rest)
            if day_match:
                day_of_week = day_map.get(day_match.group(1).lower(), 0)
                text_after = rest[day_match.end():]
                push(current_week * 7 + day_of_week, text_after)
            continue

        # Línea que empieza con día (o contiene un día). Tomamos el primer día.
        day_match = day_re.search(line)
        if day_match:
            day_of_week = day_map.get(day_match.group(1).lower(), 0)
            text_after = line[day_match.end():]
            push(current_week * 7 + day_of_week, text_after)

    for day_offset, text in sorted(seen.items()):
        sessions.append({
            "day_offset": day_offset,
            "sport": "running",
            "name": text[:50].strip(),
            "description": text,
        })
    return sessions


def _parse_distance_to_m(text: str) -> float | None:
    """Convierte una distancia textual (km o m) a metros."""
    m_km = re.search(r'(\d+(?:\.\d+)?)\s*(km|kilómetros?|kilometros?)', text, re.I)
    if m_km:
        return float(m_km.group(1)) * 1000
    m_m = re.search(r'(\d+(?:\.\d+)?)\s*m\b', text, re.I)
    if m_m:
        return float(m_m.group(1))
    return None


def _parse_duration_min(text: str) -> float | None:
    """Convierte una duración textual (min o ' horizonte) a minutos. Solo 'min'/'m' claros."""
    m_min = re.search(r'(\d+(?:\.\d+)?)\s*(?:min|mins|minutes?|minutos?)\b', text, re.I)
    if m_min:
        return float(m_min.group(1))
    return None


def _parse_workout_steps_text(desc: str) -> list[dict[str, Any]]:
    """Convierte una descripción de entrenamiento en una lista de pasos normalizados.

    Entiende calentamiento, series con repeticiones (3x800m, 4x1km, 5x200m),
    descansos, zonas y vuelta a la calma. Cada serie se expande en sus repeticiones
    (interval + descanso) para que Garmin la registre correctamente.
    """
    parts = [p.strip() for p in re.split(r'[,\n;]+', desc) if p.strip()]
    steps: list[dict[str, Any]] = []
    pending_zone: int | None = None

    for part in parts:
        lower = part.lower()

        # Captura un "Z4" suelto (zona de intensidad que se aplica a la serie anterior).
        zone_match = re.fullmatch(r'\s*z\s*(\d)\s*', lower)
        if zone_match and steps:
            pending_zone = int(zone_match.group(1))
            steps[-1]["target_hr_zone"] = pending_zone
            continue

        # ---- Series con repeticiones: 3x800m, 4x1km, 5x200m, 3 x 800 m ----
        series_match = re.search(
            r'(\d+)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(km|kilómetros?|kilometros?|m\b|min(?:uto?s)?\b)',
            part, re.I,
        )
        if series_match:
            reps = int(series_match.group(1))
            value = float(series_match.group(2))
            unit = series_match.group(3).lower()
            # Zona de intensidad de la serie (puede ir en la misma parte: 4x1km Z4)
            zone_m_pre = re.search(r'[Zz]\s*(\d)', part)
            zone = int(zone_m_pre.group(1)) if zone_m_pre else pending_zone
            # Descanso entre repeticiones: "400m rec", "rec 400m", "descanso 2min"
            rest_m = None
            rest_min = None
            m_rest = re.search(r'(?:rec|rest|descanso|recuperación)\s*(?:de\s*)?(\d+(?:\.\d+)?)\s*(km|m\b|min(?:uto?s)?\b)?', lower) or \
                     re.search(r'(\d+(?:\.\d+)?)\s*(m\b|km|min(?:uto?s)?\b)?\s*(?:rec|rest|descanso|recuperación)', lower)
            if m_rest:
                rest_num = float(m_rest.group(1))
                rest_unit = (m_rest.group(2) or "m").lower()
                if rest_unit.startswith("min"):
                    rest_min = rest_num
                elif rest_unit == "km":
                    rest_m = rest_num * 1000
                else:
                    rest_m = rest_num

            for _ in range(reps):
                step: dict[str, Any] = {"type": "interval"}
                if unit.startswith("min"):
                    step["duration_min"] = value
                elif unit == "km":
                    step["distance_km"] = value
                else:
                    step["distance_m"] = value
                if zone:
                    step["target_hr_zone"] = zone
                steps.append(step)
                if rest_m or rest_min:
                    rest_step: dict[str, Any] = {"type": "rest"}
                    if rest_min:
                        rest_step["duration_min"] = rest_min
                    else:
                        rest_step["distance_m"] = rest_m
                    steps.append(rest_step)
            pending_zone = None
            continue

        # ---- Distancia + zona (5km Z2, 800m) ----
        dist_m = _parse_distance_to_m(part)
        duration_min = _parse_duration_min(part)
        zone_m = re.search(r'[Zz]\s*(\d)', part)

        is_warmup = any(w in lower for w in ("calentamiento", "warmup", "warm up", "calentar"))
        is_cooldown = any(w in lower for w in ("vuelta a la calma", "cooldown", "cool down", "calma", "enfriamiento", "vuelta al ruedo"))
        is_rest = any(w in lower for w in ("rec", "rest", "descanso", "recuperación"))

        step: dict[str, Any] = {}
        if is_warmup:
            step["type"] = "warmup"
        elif is_cooldown:
            step["type"] = "cooldown"
        elif is_rest:
            step["type"] = "rest"
        else:
            step["type"] = "active"

        consumed = False
        if duration_min is not None:
            step["duration_min"] = duration_min
            consumed = True
        if dist_m is not None:
            if dist_m >= 1000:
                step["distance_km"] = dist_m / 1000.0
            else:
                step["distance_m"] = dist_m
            consumed = True
        if zone_m:
            step["target_hr_zone"] = int(zone_m.group(1))
        if not consumed:
            # Solo texto sin medida explícita → serie genérica de 30 min
            step["duration_min"] = 30
        steps.append(step)

    return steps


@mcp.tool
def create_workout_from_description(
    sport: str,
    name: str,
    description: str,
    target_date: str | None = None,
    auto_push_to_device: bool = False,
) -> dict[str, Any]:
    """
    Crea un entrenamiento individual desde una descripción.

    sport: "running", "cycling", "swimming", "strength_training", etc.
    name: nombre del entrenamiento.
    description: pasos del entrenamiento. Formatos aceptados:
      - JSON lista de pasos: [{"type":"warmup","duration_min":10}, {"type":"active","distance_km":5,"target_hr_zone":2}, ...]
      - Texto simple: "calentamiento 10min, 5km Z2, vuelta a la calma 5min"
    target_date: si se especifica (YYYY-MM-DD), programa el entrenamiento en esa fecha.
    auto_push_to_device: si True, envía el entrenamiento al reloj Garmin conectado.
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    steps = []
    desc = description.strip()
    if desc.startswith("["):
        try:
            steps = json.loads(desc)
        except json.JSONDecodeError:
            pass

    if not steps:
        steps = _parse_workout_steps_text(desc)

    if not steps:
        return {"error": "No pude parsear la descripción del entrenamiento"}

    workout_steps = []
    for i, s in enumerate(steps):
        workout_steps.append(_garmin_workout_step_from_desc(s, i + 1))

    sport_type_id_map = {
        "running": 1, "cycling": 2, "swimming": 4,
        "strength_training": 5, "cardio": 6, "walking": 17, "hiking": 18,
    }
    sport_key = sport.lower().replace(" ", "_")
    sport_type_id = sport_type_id_map.get(sport_key, 1)

    workout_json = {
        "workoutName": name,
        "sport": {"typeId": sport_type_id, "typeKey": sport_key},
        "workoutSteps": workout_steps,
    }

    with FETCH_LOCK:
        api = _get_api(user["id"])
        result = api.upload_workout(workout_json)

    workout_id = result.get("workoutId") if isinstance(result, dict) else None
    schedule_result = None
    if workout_id and target_date:
        with FETCH_LOCK:
            api = _get_api(user["id"])
            schedule_result = api.schedule_workout(workout_id, target_date)

    push_result = None
    if workout_id and auto_push_to_device:
        with FETCH_LOCK:
            api = _get_api(user["id"])
            try:
                push_result = api.push_workout_to_device(workout_id)
            except Exception as e:
                push_result = {"error": str(e)}

    return {
        "ok": True,
        "workout_id": workout_id,
        "scheduled": schedule_result is not None,
        "scheduled_date": target_date,
        "pushed_to_device": push_result is not None and (not isinstance(push_result, dict) or "error" not in push_result),
        "name": name,
        "sport": sport,
        "steps_count": len(steps),
    }


@mcp.tool
def create_training_plan(
    plan_name: str,
    start_date: str,
    sessions: list[dict[str, Any]],
    auto_push_to_device: bool = False,
) -> dict[str, Any]:
    """
    Crea un plan de entrenamiento completo en Garmin Connect.

    plan_name: nombre del plan (ej: "Preparación 10K - 8 semanas").
    start_date: fecha de inicio en formato YYYY-MM-DD.
    sessions: lista de sesiones, cada una con:
      - day_offset: días desde start_date (0=lunes de la semana 1)
      - sport: "running", "cycling", etc.
      - name: nombre del entrenamiento
      - steps: lista de pasos con type, duration_min/distance_km, target_hr_zone, etc.
      - description: alternativa a steps en texto libre
    auto_push_to_device: si True, envía cada entrenamiento al reloj Garmin.

    Ejemplo de sesión:
    {"day_offset": 0, "sport": "running", "name": "Rodaje suave", "steps": [
        {"type": "warmup", "duration_min": 5},
        {"type": "active", "distance_km": 5, "target_hr_zone": 2},
        {"type": "cooldown", "duration_min": 5}
    ]}
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    try:
        base_date = date.fromisoformat(start_date)
    except ValueError:
        return {"error": f"Fecha inválida: {start_date}. Usa formato YYYY-MM-DD."}

    results = []
    errors = []

    for i, session in enumerate(sessions):
        try:
            day_offset = session.get("day_offset", i)
            target_date = (base_date + timedelta(days=day_offset)).isoformat()
            sport = session.get("sport", "running")
            name = session.get("name", f"Entrenamiento {i + 1}")
            steps = session.get("steps")
            description = session.get("description", "")

            if steps:
                steps_json = json.dumps(steps)
            elif description:
                steps_json = description
            else:
                steps_json = json.dumps([{"type": "active", "duration_min": 30}])

            result = create_workout_from_description(
                sport=sport,
                name=name,
                description=steps_json,
                target_date=target_date,
                auto_push_to_device=auto_push_to_device,
            )
            results.append({"session": i + 1, "date": target_date, **result})
        except Exception as e:
            errors.append({"session": i + 1, "error": str(e)})

    return {
        "ok": len(errors) == 0,
        "plan_name": plan_name,
        "start_date": start_date,
        "total_sessions": len(sessions),
        "created": len(results),
        "errors_count": len(errors),
        "results": results,
        "errors": errors,
    }


@mcp.tool
def import_natural_language_plan(
    plan_text: str,
    start_date: str,
    plan_name: str = "Plan de entrenamiento",
    auto_push_to_device: bool = False,
) -> dict[str, Any]:
    """
    Importa un plan de entrenamiento en lenguaje natural y lo crea en Garmin Connect.

    plan_text: descripción del plan en texto libre. Formato esperado:
      "Semana 1: Lunes 5km suave Z2, Miércoles 3x800m con 400m rec, Viernes 8km largo
       Semana 2: Lunes 6km suave, Miércoles 4x1km Z4, Sábado 10km largo"

    start_date: fecha de inicio (YYYY-MM-DD). Las semanas se computan desde esta fecha.
    plan_name: nombre del plan.
    auto_push_to_device: si True, envía cada entrenamiento al reloj Garmin.
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    try:
        base_date = date.fromisoformat(start_date)
    except ValueError:
        return {"error": f"Fecha inválida: {start_date}"}

    sessions = []
    lines = [l.strip() for l in plan_text.strip().splitlines() if l.strip()]

    current_week = 0
    for line in lines:
        week_match = re.match(r'(?:semana|week)\s*(\d+)', line, re.I)
        if week_match:
            current_week = int(week_match.group(1)) - 1

        parts = [p.strip() for p in re.split(r'[,;]+', line) if p.strip()]
        for part in parts:
            if re.match(r'(?:semana|week)\s*\d+', part, re.I):
                continue
            day_match = re.match(r'(lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo|mon|tue|wed|thu|fri|sat|sun)\s*[:\-]?\s*', part, re.I)
            day_name = ""
            clean_part = part
            if day_match:
                day_name = day_match.group(1).lower()
                clean_part = part[day_match.end():]
            day_map = {"lunes": 0, "mon": 0, "martes": 1, "tue": 1, "miércoles": 2, "miercoles": 2, "wed": 2, "jueves": 3, "thu": 3, "viernes": 4, "fri": 4, "sábado": 5, "sabado": 5, "sat": 5, "domingo": 6, "sun": 6}
            day_of_week = day_map.get(day_name, len(sessions) % 7)
            week_offset = current_week * 7 + day_of_week

            sessions.append({
                "day_offset": week_offset,
                "sport": "running",
                "name": clean_part[:50].strip(),
                "description": clean_part,
            })

    if not sessions:
        return {"error": "No pude parsear el plan de entrenamiento. Asegúrate de usar el formato 'Semana X: Día descripción'."}

    return create_training_plan(
        plan_name=plan_name,
        start_date=start_date,
        sessions=sessions,
        auto_push_to_device=auto_push_to_device,
    )


@mcp.tool
def check_plan_adherence(
    start_date: str,
    end_date: str | None = None,
) -> dict[str, Any]:
    """
    Verifica el cumplimiento del plan de entrenamiento comparando workouts programados vs actividades realizadas.

    start_date: inicio del periodo (YYYY-MM-DD).
    end_date: fin del periodo (YYYY-MM-DD). Si no se especifica, usa start_date.
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    if not end_date:
        end_date = start_date

    with FETCH_LOCK:
        api = _get_api(user["id"])
        try:
            start_parts = start_date.split("-")
            end_parts = end_date.split("-")
            scheduled = api.get_scheduled_workouts(start_parts[0], start_parts[1])
        except Exception as e:
            return {"error": f"Error obteniendo workouts programados: {e}"}

    scheduled_list = []
    if isinstance(scheduled, dict):
        scheduled_list = scheduled.get("calendarItems", [])
    elif isinstance(scheduled, list):
        scheduled_list = scheduled

    completed = []
    with FETCH_LOCK:
        api = _get_api(user["id"])
        try:
            activities, _ = _optional_call_first(api, ("get_activities_by_date",), start_date, end_date)
            if isinstance(activities, list):
                completed = activities
        except Exception:
            pass

    return {
        "period": {"start": start_date, "end": end_date},
        "scheduled_count": len(scheduled_list),
        "completed_count": len(completed),
        "adherence_pct": round(len(completed) / max(1, len(scheduled_list)) * 100, 1),
        "scheduled": [{"date": s.get("date"), "name": s.get("workoutName", "")} for s in scheduled_list[:20]],
        "completed": [{"date": a.get("startTimeLocal", "")[:10], "name": a.get("activityName", "")} for a in completed[:20]],
    }


@mcp.tool
def get_weekly_report() -> dict[str, Any]:
    """Genera un informe resumen de la semana actual (lunes-domingo)."""
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    today = _today_local()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    with FETCH_LOCK:
        api = _get_api(user["id"])
        try:
            activities, _ = _optional_call_first(api, ("get_activities_by_date",), monday.isoformat(), sunday.isoformat())
        except Exception:
            activities = []

    if not isinstance(activities, list):
        activities = []

    total_km = sum((a.get("distance") or 0) / 1000 for a in activities)
    total_duration_min = sum((a.get("duration") or 0) / 60 for a in activities)
    total_calories = sum(a.get("calories") or 0 for a in activities)
    by_type: dict[str, dict[str, Any]] = {}
    for a in activities:
        t = a.get("activityType", {}).get("typeKey", "other")
        if t not in by_type:
            by_type[t] = {"count": 0, "km": 0, "min": 0}
        by_type[t]["count"] += 1
        by_type[t]["km"] += (a.get("distance") or 0) / 1000
        by_type[t]["min"] += (a.get("duration") or 0) / 60

    return {
        "week": {"start": monday.isoformat(), "end": sunday.isoformat()},
        "summary": {
            "total_activities": len(activities),
            "total_km": round(total_km, 2),
            "total_duration_min": round(total_duration_min, 1),
            "total_calories": total_calories,
        },
        "by_type": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_type.items()},
        "activities": [_normalize_activity(a) for a in activities[:10]],
    }


@mcp.tool
def predict_race_distance(distance_km: float) -> dict[str, Any]:
    """
    Predice tiempos de carrera basándose en el VO2max y entrenamientos recientes.

    distance_km: distancia objetivo (5, 10, 21.1, 42.2, etc.)
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    with FETCH_LOCK:
        api = _get_api(user["id"])
        max_metrics, _ = _optional_call_first(api, ("get_max_metrics",), _today_local().isoformat())
        activities, _ = _optional_call_first(api, ("get_activities",), 0, 20)

    vo2max = None
    if isinstance(max_metrics, dict):
        vo2max = max_metrics.get("vo2MaxValue") or max_metrics.get("vo2max")

    recent_runs = []
    if isinstance(activities, list):
        for a in activities:
            if a.get("activityType", {}).get("typeKey") in ("running", "trail_running", "treadmill_running"):
                recent_runs.append(a)

    predictions = {}
    if vo2max and float(vo2max) > 0:
        vo2 = float(vo2max)
        factor_map = {5: 0.21, 10: 0.43, 21.1: 0.93, 42.2: 1.96}
        factor = factor_map.get(distance_km)
        if factor:
            approx_seconds = (factor * 3600) / (vo2 / 35.0)
            hours = int(approx_seconds // 3600)
            mins = int((approx_seconds % 3600) // 60)
            secs = int(approx_seconds % 60)
            predictions["vo2max_based"] = f"{hours}:{mins:02d}:{secs:02d}"

    pace_per_km = None
    if predictions.get("vo2max_based"):
        parts = predictions["vo2max_based"].split(":")
        total_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        pace_sec = total_sec / distance_km
        pace_min = int(pace_sec // 60)
        pace_s = int(pace_sec % 60)
        pace_per_km = f"{pace_min}:{pace_s:02d}/km"

    return {
        "distance_km": distance_km,
        "vo2max": vo2max,
        "predictions": predictions,
        "pace_per_km": pace_per_km,
        "recent_runs_analyzed": len(recent_runs),
    }


# === ROUTE GENERATION TOOLS ===

_route_graph_cache_data: dict[str, Any] = {}
_route_graph_cache_lock = threading.Lock()


def _get_route_graph(lat: float, lon: float, radius_m: int = 15000, network_type: str = "walk") -> Any:
    """Descarga y cachea el grafo de caminos de OpenStreetMap."""
    import osmnx as ox
    import networkx as nx

    cache_key = f"{round(lat, 3)}_{round(lon, 3)}_{radius_m}_{network_type}"
    with _route_graph_cache_lock:
        cached = _route_graph_cache_data.get(cache_key)
        if cached and time.time() - cached.get("ts", 0) < 3600:
            return cached["graph"]

    try:
        G = ox.graph_from_point((lat, lon), dist=radius_m, network_type=network_type)
        G = ox.add_edge_speeds(G)
        G = ox.add_edge_travel_times(G)
    except Exception as e:
        raise RuntimeError(f"Error descargando grafo OSM: {e}")

    with _route_graph_cache_lock:
        _route_graph_cache_data[cache_key] = {"graph": G, "ts": time.time()}

    return G


def _generate_loop_route(
    lat: float,
    lon: float,
    target_distance_km: float,
    elevation_gain_target: int = 0,
    network_type: str = "walk",
    max_results: int = 3,
) -> list[dict[str, Any]]:
    """Genera rutas circulares desde un punto dado."""
    import osmnx as ox
    import networkx as nx
    from math import radians, sin, cos, sqrt, atan2

    G = _get_route_graph(lat, lon, network_type=network_type)
    orig_node = ox.distance.nearest_nodes(G, lon, lat)

    target_m = target_distance_km * 1000
    tolerance = target_m * 0.15

    all_nodes = list(G.nodes(data=True))
    candidates = []
    for node_id, data in all_nodes:
        node_lat = data.get("y", data.get("lat", 0))
        node_lon = data.get("x", data.get("lon", 0))
        dlat = radians(node_lat - lat)
        dlon = radians(node_lon - lon)
        a = sin(dlat/2)**2 + cos(radians(lat)) * cos(radians(node_lat)) * sin(dlon/2)**2
        dist = 2 * 6371000 * atan2(sqrt(a), sqrt(1-a))
        if target_m * 0.3 < dist < target_m * 0.7:
            candidates.append((node_id, dist))

    candidates.sort(key=lambda x: x[1])

    results = []
    for cand_node, _ in candidates[:min(10, len(candidates))]:
        try:
            path_out = nx.shortest_path(G, orig_node, cand_node, weight="length")
            path_back = nx.shortest_path(G, cand_node, orig_node, weight="length")

            total_dist = 0
            coords = []
            seen = set()
            for node in path_out + path_back[1:]:
                if node in seen:
                    continue
                seen.add(node)
                nd = G.nodes[node]
                coords.append({"lat": nd.get("y", nd.get("lat", 0)), "lon": nd.get("x", nd.get("lon", 0))})
                total_dist += 1

            total_dist_m = sum(
                G.edges[path_out[i], path_out[i+1], 0].get("length", 0)
                for i in range(len(path_out)-1)
            ) + sum(
                G.edges[path_back[i], path_back[i+1], 0].get("length", 0)
                for i in range(len(path_back)-1)
            )

            deviation = abs(total_dist_m - target_m)
            if deviation > tolerance:
                continue

            results.append({
                "distance_km": round(total_dist_m / 1000, 2),
                "distance_m": round(total_dist_m),
                "elevation_gain_m": elevation_gain_target,
                "points": coords,
                "point_count": len(coords),
                "deviation_pct": round(deviation / target_m * 100, 1),
            })
        except Exception:
            continue

    results.sort(key=lambda r: r["deviation_pct"])
    return results[:max_results]


@mcp.tool
def suggest_routes(
    distance_km: float,
    lat: float,
    lon: float,
    elevation_gain_m: int = 0,
    sport: str = "running",
    surface: str = "any",
    max_results: int = 3,
    max_distance_tolerance_km: float = 0,
) -> dict[str, Any]:
    """
    Genera rutas circular desde un punto, filtrando por distancia y tipo de terreno.

    distance_km: distancia objetivo en kilómetros.
    lat, lon: punto de inicio (coordenadas GPS).
    elevation_gain_m: desnivel positivo objetivo en metros (0 = sin preferencia).
    sport: "running" o "cycling".
    surface: "any", "trail", "road", "mixed".
    max_results: número máximo de rutas a devolver.
    max_distance_tolerance_km: tolerancia máxima de desviación (0 = 10% automático).
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    network_type = "bike" if sport == "cycling" else "walk"

    try:
        routes = _generate_loop_route(
            lat=lat,
            lon=lon,
            target_distance_km=distance_km,
            elevation_gain_target=elevation_gain_m,
            network_type=network_type,
            max_results=max_results,
        )
    except Exception as e:
        return {"error": f"Error generando rutas: {e}"}

    for i, route in enumerate(routes):
        route["name"] = f"Ruta {i + 1}: {route['distance_km']}km"
        route["description"] = f"Ruta circular de {route['distance_km']}km con {route['point_count']} puntos GPS"

    return {
        "query": {
            "distance_km": distance_km,
            "lat": lat,
            "lon": lon,
            "sport": sport,
            "surface": surface,
        },
        "routes": routes,
        "count": len(routes),
    }


@mcp.tool
def suggest_routes_from_profile(
    distance_km: float,
    elevation_gain_m: int = 0,
    sport: str = "running",
    surface: str = "any",
    max_results: int = 3,
) -> dict[str, Any]:
    """
    Genera rutas desde la ubicación base del usuario (home_lat/home_lon del perfil).
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    home_lat = user.get("home_lat")
    home_lon = user.get("home_lon")
    if not home_lat or not home_lon:
        return {"error": "No tienes ubicación base configurada. Usa update_user_profile para establecer home_lat y home_lon."}

    return suggest_routes(
        distance_km=distance_km,
        lat=home_lat,
        lon=home_lon,
        elevation_gain_m=elevation_gain_m,
        sport=sport,
        surface=surface,
        max_results=max_results,
    )


@mcp.tool
def export_route_gpx(route_data: dict) -> dict[str, Any]:
    """
    Exporta una ruta como archivo GPX en base64.

    route_data: dict con la ruta devuelta por suggest_routes (debe contener 'points').
    """
    import gpxpy
    import gpxpy.gpx

    points = route_data.get("points", [])
    if not points:
        return {"error": "La ruta no tiene puntos GPS"}

    gpx = gpxpy.gpx.GPX()
    gpx.name = route_data.get("name", "Ruta Garmin Coach")
    gpx.description = route_data.get("description", "")

    track = gpxpy.gpx.GPXTrack()
    gpx.tracks.append(track)
    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)

    for pt in points:
        segment.points.append(gpxpy.gpx.GPXTrackPoint(pt["lat"], pt["lon"]))

    gpx_xml = gpx.to_xml()
    b64 = base64.b64encode(gpx_xml.encode("utf-8")).decode("ascii")

    return {
        "ok": True,
        "gpx_base64": b64,
        "gpx_size_bytes": len(gpx_xml),
        "point_count": len(points),
    }


@mcp.tool
def upload_route_to_garmin(
    route_data: dict,
    name: str,
    description: str = "",
) -> dict[str, Any]:
    """
    Sube una ruta como course a Garmin Connect.

    route_data: dict con la ruta devuelta por suggest_routes.
    name: nombre de la ruta en Garmin.
    description: descripción de la ruta.
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    points = route_data.get("points", [])
    if not points:
        return {"error": "La ruta no tiene puntos GPS"}

    try:
        import gpxpy
        import gpxpy.gpx

        gpx = gpxpy.gpx.GPX()
        gpx.name = name
        gpx.description = description
        track = gpxpy.gpx.GPXTrack()
        gpx.tracks.append(track)
        segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(segment)
        for pt in points:
            segment.points.append(gpxpy.gpx.GPXTrackPoint(pt["lat"], pt["lon"]))

        gpx_xml = gpx.to_xml()
        with _tempfile.NamedTemporaryFile(suffix=".gpx", delete=False, mode="w") as f:
            f.write(gpx_xml)
            tmp_path = f.name

        with FETCH_LOCK:
            api = _get_api(user["id"])
            try:
                result = api.upload_activity(tmp_path)
            finally:
                import os
                os.unlink(tmp_path)

        return {"ok": True, "result": result, "name": name}
    except Exception as e:
        return {"error": f"Error subiendo ruta: {e}"}


@mcp.tool
def get_gear_list() -> dict:
    """Equipo deportivo registrado en Garmin (zapatillas, bicis, etc.) con su kilometraje acumulado.
    Útil para detectar desgaste (p. ej. zapatillas cerca de su vida útil).
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    with FETCH_LOCK:
        api = _get_api(user["id"])
        data, err = _optional_call_first(api, ("get_gear_stats",))

    if data is None:
        data, err = _optional_call_first(api, ("get_gear",))

    if data is None:
        return {"error": err or "No se pudo obtener el equipo deportivo"}

    gears = data if isinstance(data, list) else data.get("gearDTOs") or data.get("gear") or []
    items = []
    if isinstance(gears, list):
        for g in gears:
            if not isinstance(g, dict):
                continue
            items.append({
                "id": g.get("gearPk") or g.get("gearId"),
                "name": g.get("displayName") or g.get("name"),
                "type": g.get("type") or g.get("gearType"),
                "total_distance_km": round(_to_float_or_none(g.get("totalDistance")) / 1000, 1) if g.get("totalDistance") is not None else None,
                "custom_make": g.get("customMake"),
                "custom_model": g.get("customModel"),
                "applicable_activities": g.get("applicableActivities"),
            })
    return {"gear_list": items, "count": len(items), "raw": data}


@mcp.tool
def get_sync_status() -> dict:
    """Estado de la última sincronización del dispositivo Garmin con Garmin Connect."""
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    with FETCH_LOCK:
        api = _get_api(user["id"])
        data, err = _optional_call_first(api, ("get_device_last_used",))

    if data is None:
        return {"error": err or "No se pudo obtener el estado de sincronización"}

    last_used = data
    if isinstance(last_used, dict):
        last = last_used.get("lastUsed") or last_used.get("lastUsedTimestamp") or last_used.get("last_exercise_time")
        return {
            "sincronizado": last_used,
            "dispositivo": last_used.get("deviceName") if isinstance(last_used, dict) else None,
            "last_sync": last_used.get("lastUsedDate") if isinstance(last_used, dict) else None,
            "raw": data,
        }
    return {"synchronization_status": data}


@mcp.tool
def calculate_training_load(target_date: str = None) -> dict:
    """Carga de entrenamiento: carga aguda (últimos 7 días), crónica y balance, más estado actual y VO2max.
    Reutiliza los datos de estado de entrenamiento de Garmin.
    Formato fecha: YYYY-MM-DD (por defecto hoy).
    """
    parsed = _parse_date(target_date)
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    with FETCH_LOCK:
        api = _get_api(user["id"])
        data, err = _optional_call_first(api, ("get_training_status",), parsed)

    if data is None:
        return {"error": err or f"No hay estado de entrenamiento para {parsed}."}

    def _climb(mapping, *keys):
        node = mapping
        for k in keys:
            if not isinstance(node, dict):
                return None
            node = node.get(k)
        return node

    load = {
        "date": parsed,
        "training_status": None,
        "dashboard": None,
    }

    try:
        dash = _climb(data, "mostRecentTrainingStatus", "dashboardTrainingStatusData") or {}
        load["dashboard"] = dash
        load["training_status"] = (dash.get("trainingStatus") or {}).get("typeKey")
        load["training_load"] = _climb(dash, "trainingLoad", "current") or dash.get("trainingLoad")
        load["focus"] = _climb(dash, "trainingFocus", "typeKey")
        load["vo2max_running"] = _climb(dash, "lastRunning", "vo2Max", "value") or _climb(dash, "lastRunning", "vo2Max")
        load["vo2max_cycling"] = _climb(dash, "lastCycling", "vo2Max", "value") or _climb(dash, "lastCycling", "vo2Max")
    except Exception:
        pass

    try:
        bal = _climb(data, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap") or {}
        if isinstance(bal, dict) and bal:
            key = next(iter(bal.values()))
            load["load_balance"] = key
            load["acute_load"] = key.get("acuteLoad")
            load["chronic_load"] = key.get("chronicLoad")
            load["train_load_ratio"] = key.get("trainingLoadRatio")
            load["load_balance_commentary"] = key.get("loadBalanceCommentary") or key.get("loadBalanceCommentaryShort")
    except Exception:
        pass

    load["raw"] = data
    return load


@mcp.tool
def detect_fatigue_risk(target_date: str = None) -> dict:
    """Analiza si el usuario está listo para entrenar fuerte o debería descansar.
    Combina HRV (VFC), preparación para entrenar y calidad del sueño.
    Formato fecha: YYYY-MM-DD (por defecto hoy). Devuelve un veredicto y recomendación en texto.
    """
    parsed = _parse_date(target_date)
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    with FETCH_LOCK:
        api = _get_api(user["id"])
        hrv, hrv_err = _optional_call_first(api, ("get_hrv_data",), parsed)
        ready, ready_err = _optional_call_first(api, ("get_morning_training_readiness",), parsed)
        sleep, sleep_err = _optional_call_first(api, ("get_sleep_data",), parsed)

    hrv_status = None
    try:
        if isinstance(hrv, dict) and hrv.get("hrvSummary"):
            hrv_status = hrv["hrvSummary"].get("hrvStatus") or hrv["hrvSummary"].get("lastNightStatus")
        elif isinstance(hrv, dict):
            hrv_status = hrv.get("hrvStatus")
    except Exception:
        pass

    readiness_score = None
    readiness_short = None
    try:
        if isinstance(ready, dict):
            readiness_score = ready.get("score") or (ready.get("trainingReadinessScore") or {}).get("score")
            stages = (ready.get("trainingReadiness") or {}).get("trainingReadinessStageEnum") if isinstance(ready.get("trainingReadiness"), dict) else None
            if stages:
                other = stages.get("otherStageDTO")
                if isinstance(other, dict):
                    readiness_short = other.get("shortDescription")
    except Exception:
        pass

    sleep_minutes = None
    sleep_quality = None
    try:
        if isinstance(sleep, dict):
            sleep_minutes = sleep.get("sleepTime") or sleep.get("sleepingTime")
            sleep_quality = sleep.get("sleepQuality") or sleep.get("restingHeartRate")
    except Exception:
        pass

    # Veredicto sencillo basado en los marcadores disponibles
    markers = []
    score = 50
    if hrv_status:
        hrv_lower = str(hrv_status).lower()
        if "good" in hrv_lower or "balanced" in hrv_lower or "equilibr" in hrv_lower:
            score += 20
            markers.append(f"HRV: {hrv_status} (bien)")
        elif "poor" in hrv_lower or "low" in hrv_lower or "deficient" in hrv_lower or "desequilibrado" in hrv_lower:
            score -= 20
            markers.append(f"HRV: {hrv_status} (cuidado)")
        else:
            markers.append(f"HRV: {hrv_status}")
    if readiness_score is not None:
        score = score * 0.5 + int(readiness_score) * 0.5
        markers.append(f"Preparación: {readiness_score}/100")
        if readiness_short:
            markers.append(f"Recomendación Garmin: {readiness_short}")

    if score >= 70:
        verdict = "listo"
        advice = "Puedes hacer una sesión exigente hoy."
    elif score >= 45:
        verdict = "moderado"
        advice = "Entrena, pero baja un poco la intensidad o acorta la sesión."
    else:
        verdict = "descanso"
        advice = "Mejor descanso o muy suave. Prioriza dormir y recuperarte."

    return {
        "date": parsed,
        "verdict": verdict,
        "advice": advice,
        "markers": markers,
        "hrv_status": hrv_status,
        "readiness_score": readiness_score,
        "readiness_short": readiness_short,
        "sleep_minutes": sleep_minutes,
        "sleep_quality": sleep_quality,
    }


@mcp.tool
def summarize_period(start_date: str, end_date: str = None) -> dict:
    """Resumen de entrenamiento de un rango de fechas: sesiones, distancia, horas, subida y carga.
    Útil para informes semanales o mensuales.
    Formato fechas: YYYY-MM-DD. Si no se da end_date, usa un solo día.
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    try:
        start = date.fromisoformat(start_date)
    except ValueError:
        return {"error": f"Fecha inválida: {start_date}"}
    if not end_date:
        end = start
    else:
        try:
            end = date.fromisoformat(end_date)
        except ValueError:
            return {"error": f"Fecha inválida: {end_date}"}
    if end < start:
        start, end = end, start

    start_ts = start.isoformat()
    end_ts = end.isoformat()

    with FETCH_LOCK:
        api = _get_api(user["id"])
        activities, err = _optional_call_first(api, ("get_activities",), 0, 500)

    if activities is None:
        if err and "predisposici" in err.lower():
            activities = []
        else:
            return {"error": err or "No se pudieron leer las actividades"}

    total_count = 0
    total_distance_km = 0.0
    total_duration_min = 0.0
    total_elevation_m = 0.0
    total_calories = 0
    by_type: dict[str, dict] = {}
    days: set[str] = set()

    for a in activities if isinstance(activities, list) else []:
        if not isinstance(a, dict):
            continue
        st = a.get("startTimeLocal") or ""
        day_str = str(st)[:10] if st else ""
        if not day_str or not (start_ts <= day_str <= end_ts):
            continue
        norm = _normalize_activity(a)
        total_count += 1
        total_distance_km += norm.get("distance_km") or 0
        total_duration_min += norm.get("duration_min") or 0
        total_elevation_m += norm.get("elevation_gain_m") or 0
        total_calories += norm.get("calories") or 0
        days.add(day_str)
        t = norm.get("type") or "otro"
        if t not in by_type:
            by_type[t] = {"count": 0, "distance_km": 0.0}
        by_type[t]["count"] += 1
        by_type[t]["distance_km"] += norm.get("distance_km") or 0

    return {
        "period": {"start": start_ts, "end": end_ts, "days": (end - start).days + 1},
        "active_days": len(days),
        "sessions": total_count,
        "total_distance_km": round(total_distance_km, 2),
        "total_duration_hours": round(total_duration_min / 60, 2),
        "total_elevation_m": round(total_elevation_m, 1),
        "total_calories": total_calories,
        "by_activity_type": by_type,
    }


@mcp.tool
def calculate_pace_zones(target_date: str = None) -> dict:
    """Zonas de ritmo personalizadas para entrenamiento, según el VO2max y umbral del usuario.
    Formato fecha: YYYY-MM-DD (por defecto hoy). Devuelve zonas de 1 a 5 y el ritmo de umbral (km/min).
    """
    parsed = _parse_date(target_date)
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    vo2 = None
    with FETCH_LOCK:
        api = _get_api(user["id"])
        ts, ts_err = _optional_call_first(api, ("get_training_status",), parsed)

    try:
        if isinstance(ts, dict):
            dash = ts.get("mostRecentTrainingStatus", {}).get("dashboardTrainingStatusData", {}) or {}
            vo2 = dash.get("lastRunning", {}).get("vo2Max", {}).get("value") or dash.get("lastRunning", {}).get("vo2Max")
    except Exception:
        pass

    if not vo2:
        return {"error": "No se pudo obtener el VO2max para calcular las zonas de ritmo."}

    # Estimación del ritmo de umbral (min/km) a partir del VO2max.
    # Relación empírica validada: VDOT 40 -> ~4:25/km, VDOT 50 -> ~3:40/km.
    try:
        vdot = float(vo2) * 0.85
        pace_min_per_km = 7.42 - 0.075 * vdot
    except Exception:
        pace_min_per_km = 5.5

    def fmt(min_per_km):
        total = min_per_km * 60
        mm = int(total // 60)
        ss = int(round(total % 60))
        if ss == 60:
            mm += 1
            ss = 0
        return f"{mm}:{ss:02d}"

    return {
        "date": parsed,
        "vo2max": vo2,
        "threshold_pace_min_per_km": round(pace_min_per_km, 2),
        "threshold_pace_display": fmt(pace_min_per_km),
        "zones": {
            "zone1_recuperacion": {"pace": fmt(pace_min_per_km * 1.25), "min_per_km": round(pace_min_per_km * 1.25, 2)},
            "zone2_rodaje_suave": {"pace": fmt(pace_min_per_km * 1.15), "min_per_km": round(pace_min_per_km * 1.15, 2)},
            "zone3_aerobic": {"pace": fmt(pace_min_per_km * 1.05), "min_per_km": round(pace_min_per_km * 1.05, 2)},
            "zone4_umbral": {"pace": fmt(pace_min_per_km), "min_per_km": round(pace_min_per_km, 2)},
            "zone5_vo2max": {"pace": fmt(pace_min_per_km * 0.92), "min_per_km": round(pace_min_per_km * 0.92, 2)},
        },
    }


@mcp.tool
def plan_this_week(
    target_weekly_km: float = None,
    sessions_per_week: int = 4,
    auto_push_to_device: bool = False,
) -> dict:
    """Genera automáticamente la semana de entrenamiento, adaptada a la carga reciente para evitar sobreentrenar.
    Usa la carga y el estado de entrenamiento para proponer días y sesiones equilibrados.
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    sessions_per_week = max(2, min(6, int(sessions_per_week)))
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    start_date = monday.isoformat()

    # Estimación del volumen semanal reciente
    recent_summary = {}
    try:
        recent_summary = summarize_period((monday - timedelta(days=7)).isoformat(), (monday - timedelta(days=1)).isoformat())
    except Exception:
        pass
    recent_km = recent_summary.get("total_distance_km", 0) or 0
    if not target_weekly_km:
        target_weekly_km = round(max(15, recent_km * 1.1), 1)

    load_info = {}
    try:
        load_info = calculate_training_load(today.isoformat())
    except Exception:
        pass

    per_session = round(target_weekly_km / sessions_per_week, 2)
    day_names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

    sessions = []
    for i in range(sessions_per_week):
        day_offset = i * (7 // sessions_per_week) if sessions_per_week > 1 else 0
        day_offset = min(day_offset, 6)
        if i == sessions_per_week - 1:
            # Sesión más larga el fin de semana
            dist = round(per_session * 1.4, 2)
            kind = "Larga suave (Z2-Z3)"
            steps = json.dumps([
                {"type": "warmup", "duration_min": 10},
                {"type": "active", "distance_km": dist, "target_hr_zone": 2 if dist > 8 else 3},
                {"type": "cooldown", "duration_min": 5},
            ])
        elif i % 3 == 0:
            kind = "Ritmo / series"
            dist = round(per_session * 0.8, 2)
            steps = json.dumps([
                {"type": "warmup", "duration_min": 10},
                {"type": "interval", "distance_km": round(dist / 3, 2), "target_hr_zone": 4},
                {"type": "rest", "duration_min": 2},
                {"type": "interval", "distance_km": round(dist / 3, 2), "target_hr_zone": 4},
                {"type": "rest", "duration_min": 2},
                {"type": "interval", "distance_km": round(dist / 3, 2), "target_hr_zone": 4},
                {"type": "cooldown", "duration_min": 5},
            ])
        else:
            kind = "Rodaje suave"
            steps = json.dumps([
                {"type": "warmup", "duration_min": 10},
                {"type": "active", "distance_km": per_session, "target_hr_zone": 2},
                {"type": "cooldown", "duration_min": 5},
            ])

        sessions.append({
            "day_offset": day_offset,
            "sport": "running",
            "name": f"{kind} ({day_names[day_offset]})",
            "steps": json.loads(steps),
        })

    result = {
        "ok": True,
        "week_start": start_date,
        "target_weekly_km": target_weekly_km,
        "recent_weekly_km": round(recent_km, 2),
        "training_load": load_info.get("training_load") or load_info.get("load_balance"),
        "sessions": sessions,
        "note": "Revisa la semana propuesta. Si quieres, la genero en Garmin con create_training_plan o create_workout_from_description.",
    }
    return result


@mcp.tool
def get_todays_schedule(plan_start_date: str = None) -> dict:
    """Qu\u00e9 deber\u00edas entrenar hoy: combina el workout programado, tu carga reciente y preparaci\u00f3n.
    Devuelve una recomendaci\u00f3n accionable.
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    today = date.today().isoformat()

    scheduled = None
    try:
        with FETCH_LOCK:
            api = _get_api(user["id"])
            today_parts = today.split("-")
            sched, sched_err = _optional_call_first(api, ("get_scheduled_workouts",), today_parts[0], today_parts[1])
        if isinstance(sched, dict):
            scheduled = sched
    except Exception:
        scheduled = None

    fatigue = {}
    try:
        fatigue = detect_fatigue_risk(today)
    except Exception:
        pass

    verdict = fatigue.get("verdict", "moderado")
    advice = fatigue.get("advice", "")

    workout_today = None
    if isinstance(scheduled, dict):
        for group in (scheduled.get("workoutSchedules") or []):
            if not isinstance(group, dict):
                continue
            if str(group.get("scheduledWorkoutDate") or "")[:10] == today:
                w = group.get("workout") or {}
                workout_today = {
                    "name": w.get("workoutName"),
                    "sport": (w.get("sport") or {}).get("sportTypeKey") if isinstance(w.get("sport"), dict) else None,
                }

    plan_hint = ""
    if plan_start_date:
        try:
            base = date.fromisoformat(plan_start_date)
            delta = (date.today() - base).days
            day_index = delta % 7
            names = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
            plan_hint = f"Tu plan empezó el {plan_start_date} (día {delta + 1}). Hoy corresponde un día {names[day_index]}."
        except ValueError:
            plan_hint = ""

    return {
        "date": today,
        "verdict": verdict,
        "fatigue_advice": advice,
        "scheduled_workout_today": workout_today,
        "plan_hint": plan_hint,
        "recommendation": (
            "Sigue tu entrenamiento programado." if workout_today
            else "Hoy hay entrenamiento propuesto en tu plan/programa."
        ),
    }


@mcp.tool
def list_tools_spanish() -> dict:
    """Catálogo de todas las herramientas MCP disponibles en español, agrupadas por utilidad.
    Útil para que el asistente conozca qué acciones puede realizar con el usuario.
    """
    try:
        tools = _list_tools_sync()
    except Exception as exc:
        return {"error": f"No se pudo listar las herramientas: {exc}"}

    grouped: dict[str, list[str]] = {}
    for name, doc in tools:
        first_line = (doc or "").strip().split("\n")[0] if doc else ""
        category = "otros"
        lower = (name + " " + first_line).lower()
        if any(k in lower for k in ("sueño", "sleep", "hrv", "vfc", "body battery", "battery", "respiración", "respiration", "hidratación", "hydration", "salud")):
            category = "salud y recuperación"
        elif any(k in lower for k in ("actividad", "activity", "actividades", "entrenamiento", "workout", "plan", "series", "training")):
            category = "entrenamiento y planes"
        elif any(k in lower for k in ("ruta", "route", "gpx", "course")):
            category = "rutas"
        elif any(k in lower for k in ("usuario", "user", "api key", "perfil")):
            category = "gestión de usuarios"
        elif any(k in lower for k in ("preparación", "readiness", "carga", "load", "predisposici")):
            category = "carga y predisposición"
        grouped.setdefault(category, []).append(f"{name}: {first_line}")

    return {
        "total_tools": len(tools),
        "groups": {k: v for k, v in sorted(grouped.items())},
    }


def _list_tools_sync() -> list[tuple[str, str]]:
    import asyncio
    async def _go():
        mcp_tools = await mcp.list_tools()
        return [(t.name, t.description) for t in mcp_tools]
    return asyncio.run(_go())


@mcp.tool
def mcp_health() -> dict:
    """Estado del servicio MCP: tokens, caché, última conexión y endpoint. Útil para diagnosticar."""
    user = _get_auth_user()
    with CACHE_LOCK:
        status = CACHE.get("status")
        last_refresh = CACHE.get("last_refresh")
        last_error = CACHE.get("last_error")
    with _LAST_MCP_HIT_LOCK:
        last_mcp = _LAST_MCP_HIT
        mcp_client = _LAST_MCP_CLIENT

    connected = False
    tokens_path = None
    if user:
        tf = _user_token_file(user["id"])
        tokens_path = str(tf)
        connected = tf.exists()

    return {
        "app": APP_NAME,
        "mcp_endpoint": "/mcp",
        "cache_status": status,
        "last_refresh": last_refresh,
        "last_error": last_error,
        "auth_user": user.get("display_name") if user else None,
        "garmin_connected": connected if user else None,
        "tokens_path": tokens_path,
        "last_mcp_connection": last_mcp,
        "last_mcp_client": mcp_client,
    }


@mcp.tool
def route_to_poi(
    place: str,
    distance_km: float,
    sport: str = "running",
    surface: str = "any",
) -> dict[str, Any]:
    """Genera una ruta circular desde la ubicación base del usuario hasta un punto de interés (parque, pista...).
    Geocodifica el lugar y usa la red de calles para crear rutas de ida y vuelta.
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    home_lat = user.get("home_lat")
    home_lon = user.get("home_lon")
    if not home_lat or not home_lon:
        return {"error": "No tienes ubicación base configurada. Usa update_user_profile para establecer home_lat y home_lon."}

    try:
        import requests as _req
        resp = _req.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place, "format": "json", "limit": 1},
            headers={"User-Agent": "garmin-coach-mcp/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        return {"error": f"No se pudo geocodificar el lugar '{place}': {e}"}

    if not results:
        return {"error": f"No encontré el lugar '{place}'."}

    poi_lat = float(results[0]["lat"])
    poi_lon = float(results[0]["lon"])
    poi_name = results[0].get("display_name", place)

    network_type = "bike" if sport == "cycling" else "walk"
    try:
        routes = _generate_loop_route(
            lat=home_lat,
            lon=home_lon,
            target_distance_km=distance_km,
            elevation_gain_target=0,
            network_type=network_type,
            max_results=3,
        )
    except Exception as e:
        return {"error": f"Error generando rutas: {e}"}

    for i, route in enumerate(routes):
        route["name"] = f"Ruta hacia {poi_name[:30]} ({i + 1})"
        route["description"] = f"Ruta circular de {route['distance_km']}km pasando por {poi_name[:60]}"
        route["destination"] = {"place": poi_name, "lat": poi_lat, "lon": poi_lon}

    return {
        "query": {"place": place, "distance_km": distance_km, "sport": sport},
        "destination": {"name": poi_name[:80], "lat": poi_lat, "lon": poi_lon},
        "routes": routes,
        "count": len(routes),
        "hint": "Si una ruta te gusta, usa export_route_gpx o upload_route_to_garmin.",
    }


@mcp.tool
def generate_periodized_plan(
    race_distance_km: float,
    race_date: str,
    current_weekly_km: float = 0,
    sessions_per_week: int = 4,
    plan_name: str = "",
) -> dict[str, Any]:
    """
    Genera un plan periodizado completo para una carrera objetivo.

    race_distance_km: distancia de la carrera (5, 10, 21.1, 42.2).
    race_date: fecha de la carrera (YYYY-MM-DD).
    current_weekly_km: volumen semanal actual del usuario.
    sessions_per_week: sesiones por semana (3-6).
    plan_name: nombre del plan (se genera uno por defecto si está vacío).
    """
    user = _get_auth_user()
    if not user:
        return {"error": "No autenticado"}

    try:
        race_dt = date.fromisoformat(race_date)
    except ValueError:
        return {"error": f"Fecha inválida: {race_date}"}

    weeks_map = {5: 8, 10: 10, 21.1: 14, 42.2: 18}
    total_weeks = weeks_map.get(race_distance_km, 12)
    start_date = race_dt - timedelta(weeks=total_weeks)

    if not plan_name:
        dist_name = {5: "5K", 10: "10K", 21.1: "Media Maratón", 42.2: "Maratón"}.get(race_distance_km, f"{race_distance_km}K")
        plan_name = f"Preparación {dist_name} - {total_weeks} semanas"

    base_km = current_weekly_km if current_weekly_km > 0 else 20
    long_run_km = {5: 8, 10: 14, 21.1: 22, 42.2: 32}.get(race_distance_km, 15)

    sessions = []
    for week in range(total_weeks):
        week_num = week + 1
        phase = "base" if week < total_weeks * 0.4 else "build" if week < total_weeks * 0.75 else "taper"
        week_km = base_km
        if phase == "base":
            week_km = base_km + (base_km * 0.3 * week / (total_weeks * 0.4))
        elif phase == "build":
            peak_week = int(total_weeks * 0.75)
            week_km = base_km * 1.3 + (base_km * 0.2 * (week - total_weeks * 0.4) / (peak_week - total_weeks * 0.4))
        else:
            taper_week = total_weeks - week
            week_km = base_km * 1.5 * (1 - 0.5 * (total_weeks * 0.75 - week) / (total_weeks * 0.25))

        easy_km = week_km * 0.6 / max(1, sessions_per_week - 2)
        long_km = min(long_run_km + week_km * 0.05, week_km * 0.4)

        day_offset = week * 7
        if sessions_per_week >= 3:
            sessions.append({"day_offset": day_offset, "sport": "running", "name": f"W{week_num} Rodaje suave", "description": f"{easy_km:.1f}km Z2"})
            sessions.append({"day_offset": day_offset + 2, "sport": "running", "name": f"W{week_num} Trabajo técnico", "description": f"{easy_km * 0.8:.1f}km con progresivos"})
            sessions.append({"day_offset": day_offset + 5, "sport": "running", "name": f"W{week_num} Largo", "description": f"{long_km:.1f}km Z2"})
        if sessions_per_week >= 4:
            sessions.append({"day_offset": day_offset + 3, "sport": "running", "name": f"W{week_num} Tempo", "description": f"{easy_km * 0.7:.1f}km con 20min tempo Z3"})
        if sessions_per_week >= 5:
            sessions.append({"day_offset": day_offset + 1, "sport": "running", "name": f"W{week_num} Recuperación", "description": f"{easy_km * 0.5:.1f}km muy suave"})

    return create_training_plan(
        plan_name=plan_name,
        start_date=start_date.isoformat(),
        sessions=sessions,
        auto_push_to_device=False,
    )


# === MULTI-USER & COACHING TOOLS END ===

if __name__ == "__main__":
    _run_server()


# === FRONTEND GARMIN CONNECT ES OUTPUT PATCH START ===
_FRONTEND_EXTRA_ES_INSTRUCTIONS = (
    " Para cualquier respuesta visible para el usuario, prioriza siempre salida_frontend_es, resumen_frontend_es y metricas_frontend_es."
    " No cites ni reutilices raw_sources, códigos internos, type keys ni estados en inglés salvo que el usuario pida expresamente el dato bruto."
    " Usa exactamente el vocabulario visible de Garmin Connect en español: Body Battery, Predisposición para entrenar, Estado de VFC, Puntuación de sueño, Carga aguda, Estado de entreno, Tiempo de recuperación, Calorías, Pasos, Minutos de intensidad, Pisos y Estrés."
    " NUNCA escribas 'HRV', 'RHR', 'Training Readiness', 'Training Effect' ni 'Stamina' en la respuesta: usa 'VFC', 'FC en reposo', 'Predisposición para entrenar', 'Efecto de entrenamiento' y 'Energía disponible'."
    " Para actividades, prioriza get_activity_visible_profile y visible_metrics frente a herramientas crudas."
)
try:
    _existing_instructions = getattr(mcp, "instructions", None)
    if isinstance(_existing_instructions, str) and _FRONTEND_EXTRA_ES_INSTRUCTIONS not in _existing_instructions:
        mcp.instructions = _existing_instructions + _FRONTEND_EXTRA_ES_INSTRUCTIONS
except Exception:
    pass

_ACTIVITY_TYPE_ES = {
    "running": "Correr",
    "treadmill_running": "Correr en cinta",
    "walking": "Caminar",
    "hiking": "Senderismo",
    "trail_running": "Trail running",
    "track_running": "Carrera en pista",
    "cycling": "Ciclismo",
    "road_biking": "Ciclismo en carretera",
    "indoor_cycling": "Ciclismo indoor",
    "mountain_biking": "Ciclismo de montaña",
    "virtual_ride": "Ciclismo virtual",
    "strength_training": "Fuerza",
    "cardio": "Cardio",
    "elliptical": "Elíptica",
    "pool_swimming": "Natación en piscina",
    "open_water_swimming": "Natación en aguas abiertas",
    "swimming": "Natación",
}

_ACTIVITY_FAMILY_ES = {
    "endurance": "Resistencia",
    "cycling": "Ciclismo",
    "strength": "Fuerza",
    "swimming": "Natación",
}


def _frontend_non_empty(value: Any) -> bool:
    return value not in (None, "", [], {})


def _frontend_pick(metrics: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = metrics.get(key)
        if _frontend_non_empty(value):
            return value
    return None


def _frontend_compact_dict(items: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, value in items:
        if _frontend_non_empty(value):
            out[label] = value
    return out


def _build_metricas_frontend_es(metrics: dict[str, Any]) -> dict[str, Any]:
    return _frontend_compact_dict([
        ("Body Battery", _frontend_pick(metrics, "body_battery_actual", "body_battery_current")),
        ("Resumen de Body Battery", _frontend_pick(metrics, "body_battery_resumen_humano", "body_battery_texto")),
        ("Predisposición para entrenar", _frontend_pick(metrics, "predisposicion_para_entrenar", "training_readiness_score")),
        ("Estado de Predisposición para entrenar", _frontend_pick(metrics, "predisposicion_para_entrenar_estado", "training_readiness_status_es", "training_readiness_status")),
        ("Resumen de Predisposición para entrenar", _frontend_pick(metrics, "predisposicion_para_entrenar_texto", "predisposicion_factores_resumen_humano")),
        ("Estado de VFC", _frontend_pick(metrics, "estado_vfc", "hrv_status_es", "hrv_status")),
        ("Resumen de VFC", _frontend_pick(metrics, "estado_vfc_resumen_humano")),
        ("Puntuación de sueño", _frontend_pick(metrics, "puntuacion_de_sueno", "sleep_score")),
        ("Duración del sueño", _frontend_pick(metrics, "duracion_de_sueno_texto")),
        ("Resumen de sueño", _frontend_pick(metrics, "sueno_resumen_para_llm", "sueno_resumen_humano", "sueno_texto_seguro")),
        ("Fases del sueño", _frontend_pick(metrics, "sueno_fases_para_llm", "sueno_fases_resumen_humano")),
        ("Tiempo de recuperación", _frontend_pick(metrics, "recuperacion_texto_seguro", "training_readiness_recovery_answer_for_llm", "training_readiness_recovery_safe_text")),
        ("Carga aguda", _frontend_pick(metrics, "acute_load")),
        ("Estado de carga aguda", _frontend_pick(metrics, "acute_load_status_es", "acute_load_status")),
        ("Estado de entreno", _frontend_pick(metrics, "training_status_es", "training_status")),
        ("Resumen de estado de entreno", _frontend_pick(metrics, "estado_entreno_resumen_humano")),
        ("VO2 máximo", _frontend_pick(metrics, "vo2max")),
        ("Pasos", _frontend_pick(metrics, "steps")),
        ("Resumen de pasos", _frontend_pick(metrics, "pasos_resumen_humano")),
        ("Calorías", _frontend_pick(metrics, "calorias_resumen_humano", "total_kcal")),
        ("Minutos de intensidad", _frontend_pick(metrics, "minutos_intensidad_resumen_humano")),
        ("Estrés", _frontend_pick(metrics, "estres_resumen_humano", "stress_avg")),
        ("Última sincronización", _frontend_pick(metrics, "snapshot_obtenido_local")),
        ("Datos disponibles hasta", _frontend_pick(metrics, "datos_hasta_local")),
    ])


def _build_resumen_frontend_es(metrics: dict[str, Any]) -> list[str]:
    lines: list[str] = []

    for key in (
        "body_battery_resumen_humano",
        "predisposicion_factores_resumen_humano",
        "sueno_resumen_para_llm",
        "estado_entreno_resumen_humano",
        "estres_resumen_humano",
        "pasos_resumen_humano",
        "calorias_resumen_humano",
        "minutos_intensidad_resumen_humano",
    ):
        value = metrics.get(key)
        if _frontend_non_empty(value) and value not in lines:
            lines.append(str(value))

    if not lines:
        metricas = _build_metricas_frontend_es(metrics)
        for label, value in metricas.items():
            lines.append(f"{label}: {value}")
            if len(lines) >= 8:
                break

    return lines


def _attach_frontend_view_to_snapshot(snap: Any) -> Any:
    if not isinstance(snap, dict):
        return snap

    metrics = snap.setdefault("metrics", {})
    if not isinstance(metrics, dict):
        return snap

    replacements = {
        "stress_label": metrics.get("stress_label_es"),
        "hrv_status": _frontend_pick(metrics, "estado_vfc", "hrv_status_es"),
        "training_readiness_status": _frontend_pick(metrics, "predisposicion_para_entrenar_estado", "training_readiness_status_es"),
        "training_readiness_message": _frontend_pick(metrics, "predisposicion_para_entrenar_texto", "training_readiness_message_es"),
        "acute_load_status": metrics.get("acute_load_status_es"),
        "training_status": _frontend_pick(metrics, "training_status_es", "estado_entreno_resumen_humano"),
    }
    for base_key, value in replacements.items():
        if _frontend_non_empty(value):
            metrics[base_key] = value

    metricas_frontend_es = _build_metricas_frontend_es(metrics)
    resumen_frontend_es = _build_resumen_frontend_es(metrics)

    metrics["metricas_frontend_es"] = metricas_frontend_es
    metrics["resumen_frontend_es"] = resumen_frontend_es

    salida_frontend_es = {
        "fecha": snap.get("date"),
        "snapshot_obtenido_local": _frontend_pick(metrics, "snapshot_obtenido_local"),
        "datos_disponibles_hasta": _frontend_pick(metrics, "datos_hasta_local"),
        "metricas": metricas_frontend_es,
        "resumen": resumen_frontend_es,
    }

    ordered: dict[str, Any] = {
        "salida_frontend_es": salida_frontend_es,
        "date": snap.get("date"),
        "fetched_at": snap.get("fetched_at"),
        "metrics": metrics,
    }
    for key in ("recent_activities", "source_errors", "raw_sources"):
        if key in snap:
            ordered[key] = snap.get(key)
    for key, value in snap.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


try:
    _FRONTEND_ES_OUTPUT_ORIGINAL_COLLECT_DAY_SNAPSHOT
except NameError:
    _FRONTEND_ES_OUTPUT_ORIGINAL_COLLECT_DAY_SNAPSHOT = _collect_day_snapshot


def _collect_day_snapshot(*args, **kwargs):
    snap = _FRONTEND_ES_OUTPUT_ORIGINAL_COLLECT_DAY_SNAPSHOT(*args, **kwargs)
    return _attach_frontend_view_to_snapshot(snap)


try:
    _FRONTEND_ES_OUTPUT_ORIGINAL_NORMALIZE_ACTIVITY
except NameError:
    _FRONTEND_ES_OUTPUT_ORIGINAL_NORMALIZE_ACTIVITY = _normalize_activity


def _normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    out = _FRONTEND_ES_OUTPUT_ORIGINAL_NORMALIZE_ACTIVITY(activity)
    type_key = out.get("type")
    family = out.get("activity_family")

    out["tipo_actividad"] = _ACTIVITY_TYPE_ES.get(type_key, type_key)
    out["familia_actividad"] = _ACTIVITY_FAMILY_ES.get(family, family)

    parts: list[str] = []
    if _frontend_non_empty(out.get("tipo_actividad")):
        parts.append(str(out.get("tipo_actividad")))
    if _frontend_non_empty(out.get("distance_km")):
        parts.append(f'{out.get("distance_km")} km')
    if _frontend_non_empty(out.get("duration_min")):
        parts.append(f'{out.get("duration_min")} min')
    if _frontend_non_empty(out.get("training_load")):
        parts.append(f'carga {out.get("training_load")}')

    out["resumen_frontend_es"] = " · ".join(parts) if parts else None
    return out
# === FRONTEND GARMIN CONNECT ES OUTPUT PATCH END ===


# === STRICT GARMIN CONNECT ES TERMINOLOGY PATCH START ===
_STRICT_GARMIN_CONNECT_ES_TERMS = (
    " Si el usuario escribe en español, toda la respuesta visible debe salir en español por defecto, aunque no lo pida explícitamente."
    " Para datos de Garmin, usa exactamente la terminología visible de Garmin Connect en español y no la reformules con sinónimos."
    " Prioriza estas formas exactas: Body Battery, Predisposición para entrenar, Estado de VFC, Puntuación de sueño, Estado de entreno, Carga aguda, Tiempo de recuperación, Estrés, Calorías, Pasos y Minutos de intensidad."
    " Evita estas reformulaciones salvo que el usuario las pida expresamente o las use primero: Variabilidad de la Frecuencia Cardíaca, Estado de Entrenamiento, Preparación para entrenar, Batería corporal."
    " No uses claves internas ni términos en inglés salvo que el usuario pida el dato bruto."
    " NUNCA uses los acrónimos en inglés 'HRV', 'RHR' ni los términos 'Training Readiness', 'Training Effect' o 'Stamina' en respuestas al usuario."
    " Usa siempre: 'VFC' en lugar de 'HRV', 'FC en reposo' en lugar de 'RHR', 'Predisposición para entrenar' en lugar de 'Training Readiness', 'Efecto de entrenamiento' en lugar de 'Training Effect', y 'Energía disponible' en lugar de 'Stamina'."
    " NUNCA uses términos híbridos español-inglés como 'sobre-reach', 'over-reach' o 'overreaching': usa 'sobreentrenamiento', 'sobrecarga' o 'exceso de carga' según el contexto."
)

try:
    _current_instructions = getattr(mcp, "instructions", None)
    if isinstance(_current_instructions, str) and _STRICT_GARMIN_CONNECT_ES_TERMS not in _current_instructions:
        mcp.instructions = _current_instructions + _STRICT_GARMIN_CONNECT_ES_TERMS
except Exception:
    pass
# === STRICT GARMIN CONNECT ES TERMINOLOGY PATCH END ===
