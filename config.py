from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# App
APP_NAME = "Garmin Coach MCP"
PORT = int(os.getenv("PORT", "8000"))
CACHE_MINUTES = max(5, int(os.getenv("CACHE_MINUTES", "30")))
ACTIVITY_LIMIT = max(1, min(20, int(os.getenv("ACTIVITY_LIMIT", "8"))))
APP_TIMEZONE = ZoneInfo(os.getenv("GARMIN_TIMEZONE", "Europe/Madrid"))
GARMIN_LANGUAGE = os.getenv("GARMIN_LANGUAGE", "es").lower()

# Recovery
GARMIN_TIMEOUT_SECONDS = max(15, int(os.getenv("GARMIN_TIMEOUT_SECONDS", "45")))
# Tope máximo (segundos) que una tool call espera por FETCH_LOCK antes de
# responder "servidor ocupado" en lugar de bloquear a Claude Desktop para siempre.
FETCH_TOOL_TIMEOUT_SECONDS = max(5, int(os.getenv("FETCH_TOOL_TIMEOUT_SECONDS", "25")))
# Tope global (segundos) del prewarm inicial; pasado ese tiempo se aborta el
# primer refresh y se marca error para que /health no quede "busy" eterno.
PREWARM_MAX_SECONDS = max(60, int(os.getenv("PREWARM_MAX_SECONDS", "420")))
# Tiempo máximo (segundos) que FETCH_LOCK puede estar retenido de forma
# continuada antes de que el watchdog lo rote automáticamente. Un hilo zombi
# (p. ej. una tool call cuyo cliente abandonó la conexión) no libera nunca el
# lock y dejaría el servidor en "busy" eterno sin esta autocuración. El tope se
# fija por encima del peor caso legítimo (snapshot completo ~6 min + login).
LOCK_ROTATE_AFTER_SECONDS = max(60, int(os.getenv("LOCK_ROTATE_AFTER_SECONDS", "900")))
# Igual para REFRESH_LOCK (refresco de fondo; peor caso legítimo ~6 min).
REFRESH_LOCK_ROTATE_AFTER_SECONDS = max(
    60, int(os.getenv("REFRESH_LOCK_ROTATE_AFTER_SECONDS", "600"))
)
# Estrategias de login garminconnect que usan curl_cffi (libcurl no respeta
# socket.setdefaulttimeout y puede colgar un hilo para siempre).
GARMIN_SKIP_STRATEGIES = os.getenv(
    "GARMIN_SKIP_STRATEGIES", "mobile+cffi,widget+cffi,portal+cffi"
).strip()

RECOVERY_MAX_FRESH_MINUTES = max(15, int(os.getenv("RECOVERY_MAX_FRESH_MINUTES", "360")))
RECOVERY_CROSS_DAY_STALE_MINUTES = max(
    15, int(os.getenv("RECOVERY_CROSS_DAY_STALE_MINUTES", "180"))
)

# Auth
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
AUTH_COOKIE_MAX_AGE_SECONDS = 31536000  # 365 days
LOGIN_SESSION_TTL_SECONDS = 600
LOGIN_MFA_TIMEOUT_SECONDS = 300

# Paths
DATA_ROOT = Path(os.getenv("DATA_DIR", "/data"))
LOCAL_GARMINCONNECT_DIR = Path.home() / ".garminconnect"
LOCAL_DEBUG_TOKEN_DIR = Path.cwd() / ".debug-data" / "garmin"
USERS_DB_DIR = Path(os.getenv("USERS_DB_DIR", "/data/users"))
USERS_DB_FILE = USERS_DB_DIR / "users.json"
WEB_CONFIG_FILE = DATA_ROOT / "web-config.json"
CONFIG_SHARES_DIR = DATA_ROOT / "config-shares"


# Garmin tokens
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

# Config sharing
WEB_CONFIG_ALLOWED_KEYS = {
    "run_type",
    "distance",
    "duration",
    "pace",
    "cadence",
    "heartRate",
    "elevation",
    "weather",
    "notes",
    "lactate_threshold",
    "vo2max",
    "max_heart_rate",
}
