from __future__ import annotations

from typing import Any


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
    if latest_run:
        latest_run_load = _decision_num(latest_run.get("training_load"))
        if latest_run_load is not None and latest_run_load >= 220:
            return "La última sesión endurance fue exigente"
    if latest_strength:
        latest_strength_load = _decision_num(latest_strength.get("training_load"))
        if latest_strength_load is not None and latest_strength_load >= 60:
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
        "objetivo": "trabajar calidad con control de volumen",
        "duracion_recomendada_min": "40-60",
        "intensidad": "controlada",
        "sesion_sugerida": "tempo/umbral o fuerza principal con volumen contenido",
        "detalle": [
            "Elige una sola pieza principal y no la dupliques.",
            "Evita empezar fuerte y acabar apagado.",
            "Termina con una progresión de bajada de pulsaciones."
        ],
        "contexto_carga": f"Carga aguda {acute} ({acute_es})" if acute is not None and acute_es else acute,
    }
