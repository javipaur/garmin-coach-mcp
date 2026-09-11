from __future__ import annotations

from datetime import timedelta
from typing import Any

from config import (
    RECOVERY_CROSS_DAY_STALE_MINUTES,
    RECOVERY_MAX_FRESH_MINUTES,
)
from date_utils import _now_local, _parse_garmin_datetime, _today_local
from localization import _RECOVERY_STATE_ES


def _safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _extract_latest_activity_end_local(raw_sources: Any) -> Any:
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

            end_dt = _parse_garmin_datetime(
                activity.get("endTimeLocal") or activity.get("stopTimeLocal")
            )
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
        "training_readiness_recovery_reference_source": reference_source
        if reference_dt is not None
        else None,
        "training_readiness_recovery_reference_local": reference_dt.isoformat()
        if reference_dt is not None
        else None,
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
        is_stale = age_minutes > RECOVERY_MAX_FRESH_MINUTES or (
            crossed_local_day and age_minutes > RECOVERY_CROSS_DAY_STALE_MINUTES
        )
        if is_stale:
            state = "stale"
        elif reference_source == "last_activity_end":
            state = "estimated_from_last_activity"
        else:
            state = "fresh"

    result["training_readiness_recovery_age_minutes"] = age_minutes
    result["training_readiness_recovery_state"] = state
    result["training_readiness_recovery_state_es"] = _RECOVERY_STATE_ES.get(state, state)
    result["training_readiness_recovery_is_stale"] = state in {
        "stale",
        "missing",
        "missing_timestamp",
    }

    if raw_value is None or state in {"stale", "missing", "missing_timestamp"} or unit is None:
        result.setdefault("training_readiness_recovery_minutes_remaining", None)
        result.setdefault("training_readiness_recovery_hours_remaining", None)
        result["training_readiness_recovery_time"] = 0 if raw_value == 0 else None

        if state == "stale":
            result["training_readiness_recovery_safe_text"] = (
                "Dato de recuperación desactualizado; no extrapolar"
            )
        elif state == "missing_timestamp":
            result["training_readiness_recovery_safe_text"] = "Sin marca temporal; no extrapolar"
        else:
            result["training_readiness_recovery_safe_text"] = "Sin datos de recuperación"

        result["training_readiness_recovery_answer_for_llm"] = result[
            "training_readiness_recovery_safe_text"
        ]
        return result

    base_minutes = raw_value if unit == "minutes" else raw_value * 60.0

    remaining_minutes = max(0, int(round(base_minutes - float(age_minutes or 0))))
    remaining_hours = round(remaining_minutes / 60.0, 1)
    result["training_readiness_recovery_minutes_remaining"] = remaining_minutes
    result["training_readiness_recovery_hours_remaining"] = remaining_hours
    result["training_readiness_recovery_time"] = (
        int((remaining_minutes + 59) // 60)
        if unit in {"hours", "hours_assumed"}
        else remaining_minutes
    )

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
            result["training_readiness_recovery_safe_text"] = (
                f"Estimación: {remaining_minutes} min restantes"
            )
        else:
            result["training_readiness_recovery_safe_text"] = (
                f"Estimación: {remaining_hours} h restantes"
            )
    elif state == "stale":
        result["training_readiness_recovery_safe_text"] = (
            "Dato de recuperación desactualizado; no extrapolar"
        )
    elif state == "missing_timestamp":
        result["training_readiness_recovery_safe_text"] = "Sin marca temporal; no extrapolar"
    else:
        result["training_readiness_recovery_safe_text"] = "Sin datos de recuperación"

    result["training_readiness_recovery_answer_for_llm"] = result[
        "training_readiness_recovery_safe_text"
    ]
    return result
