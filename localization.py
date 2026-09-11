from __future__ import annotations

from typing import Any

from config import GARMIN_LANGUAGE

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
    "REST": "Descanso",
    "RECOVER": "Recuperación",
    "REPEAT": "Repetición",
    "REPEAT_STEP": "Bloque de repetición",

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

def _translate_garmin(obj: Any, _depth: int = 0) -> Any:
    """Traduce recursivamente los enums de Garmin al español de Garmin Connect."""
    if not GARMIN_LANGUAGE.startswith("es"):
        return obj
    if _depth > 50:
        return obj
    if isinstance(obj, dict):
        return {k: _translate_garmin(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_translate_garmin(i, _depth + 1) for i in obj]
    if isinstance(obj, str) and obj in _GARMIN_ES:
        return _GARMIN_ES[obj]
    return obj



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


_RECOVERY_STATE_ES = {
    "fresh": "Fresco",
    "estimated_from_last_activity": "Estimado desde la última actividad",
    "stale": "Desactualizado",
    "missing_timestamp": "Sin marca temporal",
    "missing": "Sin datos",
}


# Consolidated ES_FIELD_LABELS updates
ES_FIELD_LABELS_UPDATE = {
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
    "sueno_rem_texto": "Sueño REM",
    "sueno_profundo_texto": "Sueño profundo",
    "sueno_ligero_texto": "Sueño ligero",
    "sueno_despierto_texto": "Tiempo despierto",
    "sueno_inicio_texto": "Inicio del sueño",
    "sueno_fin_texto": "Fin del sueño",
    "sueno_fases_resumen_humano": "Resumen de fases del sueño",
    "sueno_fecha_calendario": "Fecha del sueño",
    "sueno_origen_canonico": "Origen canónico del sueño",
    "sueno_inicio_local": "Inicio local del sueño",
    "sueno_fin_local": "Fin local del sueño",
    "sueno_numero_despertares": "Número de despertares",
    "sueno_feedback_raw": "Feedback raw de sueño",
    "sueno_insight_raw": "Insight raw de sueño",
    "sueno_personalized_insight_raw": "Insight personalizado raw de sueño",
    "sueno_referencia_local": "Referencia temporal del sueño",
    "sueno_antiguedad_horas": "Antigüedad del sueño (h)",
    "sueno_estado_frescura": "Estado de frescura del sueño",
    "sueno_es_actual": "Sueño actual",
    "sueno_resumen_para_llm": "Resumen seguro de sueño para LLM",
    "sueno_fases_para_llm": "Fases de sueño seguras para LLM",
    "vfc_fecha_api_garmin": "Fecha API Garmin de VFC",
    "vfc_noche_termina_en_fecha": "Noche de VFC que termina en fecha",
    "vfc_origen_canonico": "Origen canónico de VFC",
    "vfc_referencia_texto": "Referencia humana de VFC",
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

ES_FIELD_LABELS.update(ES_FIELD_LABELS_UPDATE)


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
