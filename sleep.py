from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from config import APP_TIMEZONE
from date_utils import (
    _format_duration_hm,
    _now_local,
    _parse_garmin_datetime,
    _short_local_dt_text,
    _today_local,
)
from localization import _build_sleep_safe_text


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


def _hours_between_local_datetimes(newer: Any, older: Any) -> float | None:
    newer_dt = _parse_garmin_datetime(newer) if newer is not None else None
    older_dt = _parse_garmin_datetime(older) if older is not None else None
    if newer_dt is None or older_dt is None:
        return None
    try:
        return round((newer_dt - older_dt).total_seconds() / 3600.0, 1)
    except Exception:
        return None


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


def _find_sleep_client_in_args(*args, **kwargs):
    candidates = list(args) + list(kwargs.values())
    for obj in candidates:
        if hasattr(obj, "get_sleep_data") and callable(obj.get_sleep_data):
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
            "checked": checked,
            "selected": None,
        }

    selected = max(candidates, key=lambda c: c["end_dt"])
    return {
        "checked": checked,
        "selected": selected,
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


_SLEEP_SELECTION_DEBUG_LAST = None


def _Garmin_get_sleep_data_multi_day(self, cdate):
    from garminconnect import Garmin
    global _SLEEP_SELECTION_DEBUG_LAST

    try:
        _orig = Garmin.get_sleep_data
    except Exception:
        _orig = None

    if _orig is None:
        raise RuntimeError("No se pudo capturar Garmin.get_sleep_data original")

    requested_date = _parse_iso_date_or_today(cdate)
    now_local = _now_local()

    checked = []
    candidates = []

    offsets = (-1, 0, -2) if requested_date == _today_local() else (0, -1, -2)
    for offset in offsets:
        day = (requested_date + timedelta(days=offset)).isoformat()
        try:
            raw = _orig(self, day)
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

    return _orig(self, requested_date.isoformat())
