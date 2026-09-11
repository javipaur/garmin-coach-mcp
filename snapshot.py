from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from typing import Any

import sleep
from date_utils import (
    _format_duration_hm,
    _gsec_to_text,
    _isoish_to_local,
    _now_iso,
    _now_local,
    _parse_date,
    _parse_garmin_datetime,
    _short_local_dt_text,
)
from localization import (
    ES_FIELD_LABELS,
    _build_sleep_safe_text,
    _extract_training_status_code,
    _normalize_readiness_status_es,
    _translate_message_es,
    _translate_metric_status_es,
    _translate_status_es,
    _translate_training_readiness_message_es,
    _translate_training_status_es,
)
from recovery import _build_recovery_metrics
from sleep import (
    _apply_sleep_candidate_to_metrics,
    _epoch_millis_gmt_to_local_iso,
    _find_sleep_client_in_args,
    _hours_between_local_datetimes,
    _parse_epoch_millis_to_local_iso,
    _pick_latest_sleep_from_client,
    _recompute_sleep_freshness_fields,
)

_DEPS: dict[str, Any] = {}


def _dep(name: str) -> Any:
    return _DEPS[name]


def configure(**kwargs: Any) -> None:
    """Inyecta las dependencias de runtime que viven en server.py."""
    _DEPS.update(kwargs)


# ---------------------------------------------------------------------------
# Helpers y constantes que formaban parte del parcheo en server.py
# ---------------------------------------------------------------------------
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


def _first_present_value_sleep(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _duration_text_from_metric_keys_sleep(
    metrics: dict[str, Any], keys: tuple[str, ...]
) -> str | None:
    value = _first_present_value_sleep(metrics, keys)
    if value is None:
        return None
    return _format_duration_hm(value)


def _presentation_join(parts):
    return " · ".join([str(p) for p in parts if p not in (None, "", [], {})])


def _first_non_none_local(*values):
    for v in values:
        if v is not None:
            return v
    return None


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


# ---------------------------------------------------------------------------
# Fetch base del snapshot (extraído de server.py)
# ---------------------------------------------------------------------------
def _snapshot_base(target_date, include_recent_activities=False):
    target_date = _parse_date(target_date)
    sleep_reference_day = target_date
    with _dep("FETCH_LOCK"):
        api = _dep("_get_api")()

        summary, summary_err = _dep("_optional_call_first")(
            api, ("get_user_summary", "get_stats"), target_date
        )
        heart, heart_err = _dep("_optional_call_first")(
            api, ("get_heart_rates", "get_rhr_day"), target_date
        )
        sleep, sleep_err = _dep("_optional_call_first")(
            api, ("get_sleep_data",), sleep_reference_day
        )
        stress, stress_err = _dep("_optional_call_first")(api, ("get_stress_data",), target_date)
        body_battery, bb_err = _dep("_optional_call_first")(api, ("get_body_battery",), target_date)
        hrv, hrv_err = _dep("_optional_call_first")(api, ("get_hrv_data",), target_date)
        max_metrics, vo2_err = _dep("_optional_call_first")(api, ("get_max_metrics",), target_date)
        training_readiness, tr_err = _dep("_optional_call_first")(
            api, ("get_training_readiness",), target_date
        )
        training_status, ts_err = _dep("_optional_call_first")(
            api, ("get_training_status",), target_date
        )

        activities = []
        activities_raw = []
        activities_err = None
        if include_recent_activities:
            recent, activities_err = _dep("_optional_call_first")(
                api, ("get_activities",), 0, _dep("ACTIVITY_LIMIT")
            )
            if isinstance(recent, list):
                activities_raw = recent[: _dep("ACTIVITY_LIMIT")]
                activities = [
                    _dep("_normalize_activity")(a)
                    for a in recent[: _dep("ACTIVITY_LIMIT")]
                    if isinstance(a, dict)
                ]

        extra_raw, extra_errors, device_info = _dep("_collect_extra_raw")(
            api, target_date, training_status
        )
    metrics: dict[str, Any] = {
        "steps": (summary or {}).get("totalSteps"),
        "distance_km": round(((summary or {}).get("totalDistanceMeters") or 0) / 1000, 2),
        "active_kcal": (summary or {}).get("activeKilocalories"),
        "total_kcal": (summary or {}).get("totalKilocalories"),
        "resting_hr": _dep("_resting_hr")(heart),
        "vo2max": _dep("_extract_vo2")(max_metrics, training_status),
        "primary_device_id": device_info.get("primary_device_id"),
        "primary_device_name": device_info.get("primary_device_name"),
        "primary_device_image_url": device_info.get("primary_device_image_url"),
    }
    metrics.update(_dep("_sleep_metrics")(sleep))
    metrics.update(_dep("_stress_metrics")(stress))
    metrics.update(_dep("_body_battery_metrics")(body_battery))
    metrics.update(_dep("_hrv_metrics")(hrv))
    metrics.update(_dep("_training_readiness_metrics")(training_readiness))
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
    if raw_sources.get("device_settings_raw") is not None:
        errors.pop("device_settings_raw", None)
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


# ---------------------------------------------------------------------------
# Etiquetas ES que el monolito aplicaba al import (ES_FIELD_LABELS.update)
# ---------------------------------------------------------------------------
ES_FIELD_LABELS.update(
    {
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
    }
)

ES_FIELD_LABELS.update(
    {
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
    }
)

ES_FIELD_LABELS.update(
    {
        "sueno_rem_texto": "Sueño REM",
        "sueno_profundo_texto": "Sueño profundo",
        "sueno_ligero_texto": "Sueño ligero",
        "sueno_despierto_texto": "Tiempo despierto",
        "sueno_inicio_texto": "Inicio del sueño",
        "sueno_fin_texto": "Fin del sueño",
        "sueno_fases_resumen_humano": "Resumen de fases del sueño",
    }
)

ES_FIELD_LABELS.update(
    {
        "sueno_fecha_calendario": "Fecha del sueño",
        "sueno_origen_canonico": "Origen canónico del sueño",
        "sueno_inicio_local": "Inicio local del sueño",
        "sueno_fin_local": "Fin local del sueño",
        "sueno_numero_despertares": "Número de despertares",
        "sueno_feedback_raw": "Feedback raw de sueño",
        "sueno_insight_raw": "Insight raw de sueño",
        "sueno_personalized_insight_raw": "Insight personalizado raw de sueño",
    }
)

ES_FIELD_LABELS.update(
    {
        "sueno_referencia_local": "Referencia temporal del sueño",
        "sueno_antiguedad_horas": "Antigüedad del sueño (h)",
        "sueno_estado_frescura": "Estado de frescura del sueño",
        "sueno_es_actual": "Sueño actual",
        "sueno_resumen_para_llm": "Resumen seguro de sueño para LLM",
        "sueno_fases_para_llm": "Fases de sueño seguras para LLM",
    }
)

if "ES_FIELD_LABELS" in globals():
    ES_FIELD_LABELS.update(
        {
            "predisposicion_factores_resumen_humano": "Resumen humano de factores de Predisposición",
            "peso_referencia_texto": "Referencia del peso",
            "fitness_age_referencia_texto": "Referencia de edad física",
        }
    )

if "ES_FIELD_LABELS" in globals():
    ES_FIELD_LABELS.update(
        {
            "aclimatacion_spo2_promedio": "Promedio de SpO₂ de aclimatación",
            "aclimatacion_spo2_minima": "SpO₂ mínima de aclimatación",
            "aclimatacion_spo2_ultima": "Última SpO₂ de aclimatación",
            "aclimatacion_spo2_media_general": "SpO₂ media general",
            "aclimatacion_altitud_media_entorno": "Altitud media del entorno",
            "aclimatacion_spo2_hora_ultima_local": "Hora local de la última SpO₂",
            "aclimatacion_spo2_sueno_inicio_local": "Inicio local de sueño para SpO₂",
            "aclimatacion_spo2_sueno_fin_local": "Fin local de sueño para SpO₂",
            "aclimatacion_spo2_resumen_humano": "Resumen humano de aclimatación por pulsioximetría",
        }
    )

if "ES_FIELD_LABELS" in globals():
    ES_FIELD_LABELS.update(
        {
            "umbral_lactato_fc_ppm": "Umbral de lactato (frecuencia cardiaca)",
            "umbral_lactato_autodetectado": "Umbral de lactato autodetectado",
            "umbral_lactato_ritmo_disponible": "Ritmo de umbral disponible",
            "umbral_lactato_potencia_disponible": "Potencia de umbral disponible",
            "umbral_lactato_wkg_disponible": "Potencia relativa de umbral disponible",
            "umbral_lactato_speed_raw": "Velocidad bruta de umbral de lactato",
            "umbral_lactato_resumen_humano": "Resumen humano de umbral de lactato",
        }
    )


def _patch_garmin_metrics(snap, *args, **kwargs):
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
    _garmin_patch_put(
        metrics,
        "sleep_hours",
        round(sleep_seconds / 3600, 1) if sleep_seconds is not None else None,
    )
    _garmin_patch_put(
        metrics,
        "sleep_score",
        ((sleep_dto.get("sleepScores") or {}).get("overall") or {}).get("value"),
    )
    _garmin_patch_put(
        metrics, "sleep_deep_min", _garmin_patch_minutes(sleep_dto.get("deepSleepSeconds"))
    )
    _garmin_patch_put(
        metrics, "sleep_rem_min", _garmin_patch_minutes(sleep_dto.get("remSleepSeconds"))
    )
    _garmin_patch_put(
        metrics, "sleep_light_min", _garmin_patch_minutes(sleep_dto.get("lightSleepSeconds"))
    )
    _garmin_patch_put(
        metrics, "sleep_awake_min", _garmin_patch_minutes(sleep_dto.get("awakeSleepSeconds"))
    )
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
    _garmin_patch_put(
        metrics,
        "stress_avg",
        _garmin_patch_first_non_none(
            summary.get("averageStressLevel"), stress.get("avgStressLevel")
        ),
    )
    _garmin_patch_put(
        metrics,
        "stress_max",
        _garmin_patch_first_non_none(summary.get("maxStressLevel"), stress.get("maxStressLevel")),
    )
    _garmin_patch_put(metrics, "stress_label", stress_label)
    if stress_label is not None:
        metrics["stress_label_es"] = _GARMIN_PATCH_STRESS_LABEL_ES.get(
            stress_label, metrics.get("stress_label_es")
        )
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
        metrics["hrv_status_es"] = _GARMIN_PATCH_HRV_STATUS_ES.get(
            hrv_status, metrics.get("hrv_status_es")
        )
    if training_readiness:
        tr_status = training_readiness.get("level")
        tr_message = training_readiness.get("feedbackShort")
        _garmin_patch_put(metrics, "training_readiness_score", training_readiness.get("score"))
        _garmin_patch_put(metrics, "training_readiness_status", tr_status)
        _garmin_patch_put(metrics, "training_readiness_message", tr_message)
        _garmin_patch_put(
            metrics, "training_readiness_recovery_time", training_readiness.get("recoveryTime")
        )
        _garmin_patch_put(
            metrics, "training_readiness_input_context", training_readiness.get("inputContext")
        )
        if tr_status is not None:
            metrics["training_readiness_status_es"] = (
                _GARMIN_PATCH_TRAINING_READINESS_STATUS_ES.get(
                    tr_status,
                    metrics.get("training_readiness_status_es"),
                )
            )
        if tr_message is not None:
            metrics["training_readiness_message_es"] = (
                _GARMIN_PATCH_TRAINING_READINESS_MESSAGE_ES.get(
                    tr_message,
                    metrics.get("training_readiness_message_es"),
                )
            )
    latest_status_data = (
        (training_status.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")
    ) or {}
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
    profile_data = user_profile.get("userData") or {}
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
    _VO2MAX_CAT_ES = {
        0: "Deficiente",
        1: "Bajo",
        2: "Aceptable",
        3: "Bueno",
        4: "Excelente",
        5: "Superior",
    }
    vo2_cat = vo2_block.get("maxMetCategory")
    if vo2_cat is not None:
        _garmin_patch_put(metrics, "vo2max_label", _VO2MAX_CAT_ES.get(vo2_cat))
    respiration = raw.get("respiration_raw") or {}
    _garmin_patch_put(
        metrics, "respiration_waking_avg", respiration.get("avgWakingRespirationValue")
    )
    _garmin_patch_put(metrics, "respiration_sleep_avg", respiration.get("avgSleepRespirationValue"))
    _garmin_patch_put(metrics, "respiration_min", respiration.get("lowestRespirationValue"))
    _garmin_patch_put(metrics, "respiration_max", respiration.get("highestRespirationValue"))
    spo2 = raw.get("spo2_raw") or {}
    if isinstance(spo2, dict):
        _garmin_patch_put(metrics, "spo2_latest", spo2.get("latestSpO2"))
        _garmin_patch_put(metrics, "spo2_avg_day", spo2.get("averageSpO2"))
        _garmin_patch_put(metrics, "spo2_avg_sleep", spo2.get("avgSleepSpO2"))
        _garmin_patch_put(metrics, "spo2_min", spo2.get("lowestSpO2"))
        _garmin_patch_put(metrics, "spo2_7d_avg", spo2.get("lastSevenDaysAvgSpO2"))
    return snap


def _patch_es_initial(snap, *args, **kwargs):
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


def _patch_es_recheck(snap, *args, **kwargs):
    metrics = snap.get("metrics") or {}
    metrics["stress_label_es"] = _translate_status_es(metrics.get("stress_label"))
    metrics["hrv_status_es"] = _translate_status_es(metrics.get("hrv_status"))
    metrics["training_readiness_status_es"] = _translate_status_es(
        metrics.get("training_readiness_status")
    )
    metrics["training_readiness_message_es"] = _translate_message_es(
        metrics.get("training_readiness_message")
    )
    metrics["acute_load_status_es"] = _translate_status_es(metrics.get("acute_load_status"))
    snap["metrics"] = metrics
    return snap


def _patch_es_canonical(snap, *args, **kwargs):
    metrics = snap.setdefault("metrics", {})
    metrics["stress_label_es"] = _translate_status_es(metrics.get("stress_label"))
    metrics["hrv_status_es"] = _translate_status_es(metrics.get("hrv_status"))
    metrics["training_readiness_status_es"] = _translate_status_es(
        metrics.get("training_readiness_status")
    )
    metrics["training_readiness_message_es"] = _translate_message_es(
        metrics.get("training_readiness_message")
    )
    metrics["acute_load_status_es"] = _translate_status_es(metrics.get("acute_load_status"))
    return snap


def _patch_recovery_guardrails(snap, *args, **kwargs):
    metrics = snap.setdefault("metrics", {})
    raw_sources = snap.get("raw_sources") or {}
    entry = None
    selected_entry = metrics.get("training_readiness_selected_entry")
    if isinstance(selected_entry, dict):
        entry = selected_entry
    else:
        entry = _dep("_select_training_readiness_entry")(raw_sources.get("training_readiness_raw"))
    recovery_metrics = _build_recovery_metrics(entry, raw_sources)
    metrics.update(recovery_metrics)
    selected_ts = None
    if isinstance(entry, dict):
        selected_ts = entry.get("timestampLocal") or entry.get("timestamp")
    metrics["training_readiness_selected_timestamp_local"] = selected_ts
    return snap


def _patch_ui_canonical_fields(snap, *args, **kwargs):
    metrics = snap.setdefault("metrics", {})
    metrics["predisposicion_para_entrenar"] = metrics.get("training_readiness_score")
    readiness_status_es = _normalize_readiness_status_es(
        metrics.get("training_readiness_status_es") or metrics.get("training_readiness_status")
    )
    metrics["predisposicion_para_entrenar_estado"] = readiness_status_es
    metrics["predisposicion_para_entrenar_texto"] = (
        f"{metrics.get('training_readiness_score')} — {readiness_status_es}"
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
        metrics["predisposicion_factor_feedback_recuperacion_raw"] = readiness_entry.get(
            "recoveryTimeFactorFeedback"
        )
        metrics["predisposicion_factor_feedback_sueno_reciente_raw"] = readiness_entry.get(
            "sleepHistoryFactorFeedback"
        )
        metrics["predisposicion_factor_feedback_estres_reciente_raw"] = readiness_entry.get(
            "stressHistoryFactorFeedback"
        )
    metrics["body_battery_actual"] = metrics.get("body_battery_current")
    metrics["body_battery_ultimo_timestamp_local"] = metrics.get(
        "body_battery_last_timestamp_local"
    )
    metrics["body_battery_texto"] = (
        f"{metrics.get('body_battery_current')} actual"
        if metrics.get("body_battery_current") is not None
        else None
    )
    metrics["puntuacion_de_sueno"] = metrics.get("sleep_score")
    metrics["duracion_de_sueno_texto"] = _format_duration_hm(metrics.get("sleep_duration_seconds"))
    metrics["sueno_texto_seguro"] = _build_sleep_safe_text(
        metrics.get("sleep_score"),
        metrics.get("duracion_de_sueno_texto"),
    )
    metrics["recuperacion_texto_seguro"] = metrics.get(
        "training_readiness_recovery_answer_for_llm"
    ) or metrics.get("training_readiness_recovery_safe_text")
    metrics["snapshot_obtenido_local"] = _isoish_to_local(snap.get("fetched_at"))
    metrics["datos_hasta_local"] = _latest_known_data_timestamp_local(metrics)
    return snap


def _patch_human_sleep_phase_fields(snap, *args, **kwargs):
    metrics = snap.setdefault("metrics", {})
    metrics["sueno_rem_texto"] = _duration_text_from_metric_keys_sleep(
        metrics,
        (
            "sleep_rem_seconds",
            "sleep_rem_duration_seconds",
            "sleep_rem_time_seconds",
            "rem_sleep_seconds",
            "remSleepSeconds",
            "remSleepDuration",
            "rem_seconds",
        ),
    )
    metrics["sueno_profundo_texto"] = _duration_text_from_metric_keys_sleep(
        metrics,
        (
            "sleep_deep_seconds",
            "sleep_deep_duration_seconds",
            "deep_sleep_seconds",
            "deepSleepSeconds",
            "deepSleepDuration",
            "deep_seconds",
        ),
    )
    metrics["sueno_ligero_texto"] = _duration_text_from_metric_keys_sleep(
        metrics,
        (
            "sleep_light_seconds",
            "sleep_light_duration_seconds",
            "light_sleep_seconds",
            "lightSleepSeconds",
            "lightSleepDuration",
            "light_seconds",
        ),
    )
    metrics["sueno_despierto_texto"] = _duration_text_from_metric_keys_sleep(
        metrics,
        (
            "sleep_awake_seconds",
            "sleep_awake_duration_seconds",
            "awake_sleep_seconds",
            "awakeSleepSeconds",
            "awakeDuration",
            "sleep_wake_seconds",
            "awake_seconds",
        ),
    )
    sueno_inicio_raw = _first_present_value_sleep(
        metrics,
        (
            "sleep_start_local",
            "sleep_start_time_local",
            "sleep_bedtime_local",
            "sleep_start_timestamp_local",
            "sleepStartTimestampLocal",
            "sleepTimeLocal",
            "sleep_start",
        ),
    )
    sueno_fin_raw = _first_present_value_sleep(
        metrics,
        (
            "sleep_end_local",
            "sleep_end_time_local",
            "sleep_wake_time_local",
            "wake_time_local",
            "sleep_end_timestamp_local",
            "sleepEndTimestampLocal",
            "wakeTimeLocal",
            "sleep_end",
        ),
    )
    metrics["sueno_inicio_texto"] = _short_local_dt_text(_isoish_to_local(sueno_inicio_raw))
    metrics["sueno_fin_texto"] = _short_local_dt_text(_isoish_to_local(sueno_fin_raw))
    fases = []
    if metrics.get("sueno_rem_texto"):
        fases.append(f"REM {metrics.get('sueno_rem_texto')}")
    if metrics.get("sueno_profundo_texto"):
        fases.append(f"Profundo {metrics.get('sueno_profundo_texto')}")
    if metrics.get("sueno_ligero_texto"):
        fases.append(f"Ligero {metrics.get('sueno_ligero_texto')}")
    if metrics.get("sueno_despierto_texto"):
        fases.append(f"Despierto {metrics.get('sueno_despierto_texto')}")
    metrics["sueno_fases_resumen_humano"] = ", ".join(fases) if fases else None
    return snap


def _patch_raw_sleep_canonicalization(snap, *args, **kwargs):
    metrics = snap.setdefault("metrics", {})
    raw_sources = snap.get("raw_sources") or {}
    if metrics.get("snapshot_obtenido_texto") is None:
        metrics["snapshot_obtenido_texto"] = _short_local_dt_text(
            metrics.get("snapshot_obtenido_local")
        )
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
            metrics["estado_vfc_resumen_humano"] = (
                f"{estado}, {noche} ms nocturnos, {media7} ms de media 7 días"
            )
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

        start_local_iso = _parse_epoch_millis_to_local_iso(
            daily.get("sleepStartTimestampLocal")
        ) or _parse_epoch_millis_to_local_iso(daily.get("sleepStartTimestampGMT"))
        end_local_iso = _parse_epoch_millis_to_local_iso(
            daily.get("sleepEndTimestampLocal")
        ) or _parse_epoch_millis_to_local_iso(daily.get("sleepEndTimestampGMT"))

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
            fases.append(f"REM {metrics.get('sueno_rem_texto')}")
        if metrics.get("sueno_profundo_texto"):
            fases.append(f"Profundo {metrics.get('sueno_profundo_texto')}")
        if metrics.get("sueno_ligero_texto"):
            fases.append(f"Ligero {metrics.get('sueno_ligero_texto')}")
        if metrics.get("sueno_despierto_texto"):
            fases.append(f"Despierto {metrics.get('sueno_despierto_texto')}")
        metrics["sueno_fases_resumen_humano"] = ", ".join(fases) if fases else None

        # Si el fin del sueño es más reciente que el "datos_hasta_local" previo, lo actualizamos
        current_datos_hasta = (
            _parse_garmin_datetime(metrics.get("datos_hasta_local"))
            if metrics.get("datos_hasta_local")
            else None
        )
        sleep_end_dt = _parse_garmin_datetime(end_local_iso) if end_local_iso else None
        if sleep_end_dt is not None and (
            current_datos_hasta is None or sleep_end_dt > current_datos_hasta
        ):
            metrics["datos_hasta_local"] = sleep_end_dt.isoformat()
            metrics["datos_hasta_texto"] = _short_local_dt_text(metrics.get("datos_hasta_local"))
    return snap


def _patch_sleep_gmt_fix(snap, *args, **kwargs):
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


def _patch_sleep_freshness(snap, *args, **kwargs):
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
    metrics["sueno_referencia_local"] = (
        sleep_ref_dt.isoformat() if sleep_ref_dt is not None else None
    )
    metrics["sueno_antiguedad_horas"] = age_hours
    metrics["sueno_estado_frescura"] = state
    metrics["sueno_es_actual"] = state == "fresh"
    summary = metrics.get("sueno_resumen_humano") or metrics.get("sueno_texto_seguro")
    phases = metrics.get("sueno_fases_resumen_humano")
    if state == "fresh":
        metrics["sueno_resumen_para_llm"] = summary
        metrics["sueno_fases_para_llm"] = phases
    elif state == "stale":
        ref_text = _short_local_dt_text(metrics.get("sueno_referencia_local")) or metrics.get(
            "sueno_fecha_calendario"
        )
        metrics["sueno_resumen_para_llm"] = (
            f"Último sueño disponible del conector: {ref_text}; no asumir que corresponde a anoche"
        )
        metrics["sueno_fases_para_llm"] = None
    elif state == "unknown":
        ref_text = _short_local_dt_text(metrics.get("sueno_referencia_local")) or "sin fecha clara"
        metrics["sueno_resumen_para_llm"] = (
            f"Hay un sueño disponible ({ref_text}), pero no se pudo validar si corresponde a hoy"
        )
        metrics["sueno_fases_para_llm"] = None
    else:
        metrics["sueno_resumen_para_llm"] = "No hay sueño usable en el snapshot actual"
        metrics["sueno_fases_para_llm"] = None
    return snap


def _patch_multi_day_sleep_selection(snap, *args, **kwargs):
    metrics = snap.setdefault("metrics", {})
    raw_sources = snap.setdefault("raw_sources", {})
    client = _find_sleep_client_in_args(*args, **kwargs)
    snapshot_local_iso = (
        metrics.get("snapshot_obtenido_local")
        or _isoish_to_local(snap.get("fetched_at"))
        or _now_local().isoformat()
    )
    selection = None
    if client is not None:
        selection = _pick_latest_sleep_from_client(client, snapshot_local_iso)
    if isinstance(selection, dict):
        raw_sources["sleep_selection_debug"] = selection.get("checked")
    selected = selection.get("selected") if isinstance(selection, dict) else None
    if selected is not None:
        raw_sources["sleep_raw"] = selected["raw"]
        source_label = f"garmin.get_sleep_data({selected['calendar_date']})"
        _apply_sleep_candidate_to_metrics(metrics, selected, source_label)
        _recompute_sleep_freshness_fields(metrics)
    return snap


def _patch_sleep_selection_debug_bridge(snap, *args, **kwargs):
    raw_sources = snap.setdefault("raw_sources", {})
    metrics = snap.setdefault("metrics", {})
    if sleep._SLEEP_SELECTION_DEBUG_LAST is not None:
        raw_sources["sleep_selection_debug"] = deepcopy(sleep._SLEEP_SELECTION_DEBUG_LAST)
        selected = sleep._SLEEP_SELECTION_DEBUG_LAST.get("selected") or {}
        if isinstance(selected, dict):
            requested_date = selected.get("requested_date")
            calendar_date = selected.get("calendar_date")
            metrics["sueno_origen_canonico"] = (
                f"garmin.get_sleep_data multi-day ({requested_date} -> {calendar_date})"
            )
    return snap


def _patch_hrv_selection_debug_bridge(snap, *args, **kwargs):
    raw_sources = snap.setdefault("raw_sources", {})
    metrics = snap.setdefault("metrics", {})
    if _dep("_get_hrv_debug_last")() is not None:
        raw_sources["hrv_selection_debug"] = deepcopy(_dep("_get_hrv_debug_last")())
        selected = _dep("_get_hrv_debug_last")().get("selected") or {}
        if selected:
            requested_date_base = _dep("_get_hrv_debug_last")().get("requested_date_base")
            source_date = selected.get("requested_date")
            metrics["vfc_fecha_api_garmin"] = source_date
            metrics["vfc_origen_canonico"] = (
                f"garmin.get_hrv_data multi-day ({requested_date_base} -> {source_date})"
            )
            try:
                if not source_date:
                    raise ValueError
                intuitive_date = (
                    date.fromisoformat(str(source_date)) + timedelta(days=1)
                ).isoformat()
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
                metrics["vfc_referencia_texto"] = (
                    f"VFC nocturna de la noche que termina el {ref_text} (fecha API Garmin: {fecha_api})"
                )
            else:
                metrics["vfc_referencia_texto"] = (
                    f"VFC nocturna de la noche que termina el {ref_text}"
                )
    return snap


def _patch_presentation_cleanup(snap, *args, **kwargs):
    metrics = snap.setdefault("metrics", {})
    raw = snap.setdefault("raw_sources", {})
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
    body = raw.get("body_composition_raw") or {}
    user = ((raw.get("user_profile_raw") or {}).get("userData")) or {}
    total_average = body.get("totalAverage") or {}
    daily_weight = total_average.get("weight")
    profile_weight = user.get("weight")
    if daily_weight is not None:
        metrics["peso_referencia_texto"] = "Peso de composición corporal del día"
    elif profile_weight is not None:
        metrics["peso_referencia_texto"] = (
            "Peso tomado del perfil de Garmin (sin medición corporal del día)"
        )
    fitness_age_raw = raw.get("fitness_age_raw") or {}
    bmi_component = (fitness_age_raw.get("components") or {}).get("bmi") or {}
    bmi_last_measurement = bmi_component.get("lastMeasurementDate")
    if metrics.get("fitness_age") is not None:
        txt = "Edad física calculada por Garmin"
        if bmi_last_measurement:
            txt += f" · IMC con última medición {bmi_last_measurement}"
        metrics["fitness_age_referencia_texto"] = txt
    return snap


def _patch_acclimatacion_spo2(snap, *args, **kwargs):
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


def _patch_lactato_parcial(snap, *args, **kwargs):
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


def _patch_ui_texts(snap, *args, **kwargs):
    metrics = snap.setdefault("metrics", {})
    raw = snap.setdefault("raw_sources", {})
    summary = raw.get("summary_raw") or {}
    load_balance_map = (
        ((raw.get("training_status_raw") or {}).get("mostRecentTrainingLoadBalance") or {}).get(
            "metricsTrainingLoadBalanceDTOMap"
        )
    ) or {}
    load_balance = None
    if isinstance(load_balance_map, dict):
        for block in load_balance_map.values():
            if isinstance(block, dict):
                load_balance = block
                break
    load_balance = load_balance or {}
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
    foco = None
    al = load_balance.get("monthlyLoadAerobicLow")
    ah = load_balance.get("monthlyLoadAerobicHigh")
    an = load_balance.get("monthlyLoadAnaerobic")
    al_max = load_balance.get("monthlyLoadAerobicLowTargetMax")
    ah_max = load_balance.get("monthlyLoadAerobicHighTargetMax")
    an_max = load_balance.get("monthlyLoadAnaerobicTargetMax")
    try:
        if (
            al is not None
            and ah is not None
            and an is not None
            and al_max is not None
            and ah_max is not None
            and an_max is not None
            and al > al_max
            and ah > ah_max
            and an > an_max
        ):
            foco = "Por encima de los objetivos"
    except Exception:
        pass
    if foco:
        metrics["foco_de_carga_texto"] = foco
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
    if "ES_FIELD_LABELS" in globals():
        ES_FIELD_LABELS.update(
            {
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
            }
        )
    return snap


def _patch_attach_frontend(snap, *args, **kwargs):
    return _dep("_attach_frontend_view_to_snapshot")(snap)


# ---------------------------------------------------------------------------
# Cadena de transformaciones del snapshot, en su orden original de aplicación.
# ---------------------------------------------------------------------------

_PATCHES = [
    _patch_garmin_metrics,
    _patch_es_initial,
    _patch_es_recheck,
    _patch_es_canonical,
    _patch_recovery_guardrails,
    _patch_ui_canonical_fields,
    _patch_human_sleep_phase_fields,
    _patch_raw_sleep_canonicalization,
    _patch_sleep_gmt_fix,
    _patch_sleep_freshness,
    _patch_multi_day_sleep_selection,
    _patch_sleep_selection_debug_bridge,
    _patch_hrv_selection_debug_bridge,
    _patch_presentation_cleanup,
    _patch_acclimatacion_spo2,
    _patch_lactato_parcial,
    _patch_ui_texts,
    _patch_attach_frontend,
]


def _collect_day_snapshot(target_date, include_recent_activities=False, *args, **kwargs):
    snap = _snapshot_base(target_date, include_recent_activities)
    for patch in _PATCHES:
        snap = patch(snap, *args, **kwargs)
    return snap
