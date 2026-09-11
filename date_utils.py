from __future__ import annotations

from datetime import date, datetime
from typing import Any

from config import APP_TIMEZONE


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
        dt = dt.replace(tzinfo=APP_TIMEZONE) if dt.tzinfo is None else dt.astimezone(APP_TIMEZONE)
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


def _parse_garmin_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        dt = datetime.fromisoformat(normalized)
        dt = dt.replace(tzinfo=APP_TIMEZONE) if dt.tzinfo is None else dt.astimezone(APP_TIMEZONE)
        return dt
    except Exception:
        return None


def _short_local_dt_text(value: Any) -> str | None:
    dt = _parse_garmin_datetime(value) if value is not None else None
    if dt is None:
        return None
    return dt.strftime("%d/%m/%Y %H:%M")


def _parse_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _gsec_to_text(seconds: float | int | None) -> str | None:
    if seconds is None:
        return None
    try:
        total = int(round(float(seconds)))
    except Exception:
        return None
    if total < 0:
        return None
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"
