from __future__ import annotations

import re
from typing import Any


def _garmin_workout_step_from_desc(step: dict[str, Any], step_order: int) -> dict[str, Any]:
    step_type = step.get("type", "active").lower()
    intensity_map = {
        "warmup": "warmup", "calentamiento": "warmup",
        "active": "interval", "interval": "interval", "intervalo": "interval",
        "rest": "recovery", "recovery": "recovery", "recuperación": "recovery", "recuperacion": "recovery",
        "cooldown": "cooldown", "vuelta a la calma": "cooldown",
    }
    intensity = intensity_map.get(step_type, "interval")

    step_type_id = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "repeat": 6}.get(intensity, 3)

    duration_value: float | None = None
    duration_type = "time"
    if "duration_sec" in step and step["duration_sec"]:
        duration_value = float(step["duration_sec"])
    elif "duration_min" in step and step["duration_min"]:
        duration_value = float(step["duration_min"]) * 60
    elif "distance_m" in step and step["distance_m"]:
        duration_type = "distance"
        duration_value = float(step["distance_m"])
    elif "distance_km" in step and step["distance_km"]:
        duration_type = "distance"
        duration_value = float(step["distance_km"]) * 1000

    result: dict[str, Any] = {
        "type": "ExecutableStepDTO",
        "stepOrder": step_order,
        "stepType": {"stepTypeId": step_type_id, "stepTypeKey": intensity, "displayOrder": step_type_id},
    }

    if duration_value is not None:
        result["endCondition"] = {
            "conditionTypeId": 2 if duration_type == "time" else 1,
            "conditionTypeKey": duration_type,
            "displayOrder": 2 if duration_type == "time" else 1,
            "displayable": True,
        }
        result["endConditionValue"] = duration_value

    hr_zone = step.get("target_hr_zone")
    target_pace = step.get("target_pace_mps")
    if hr_zone:
        result["targetType"] = {
            "workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 3,
        }
        result["targetValueOne"] = 0.0
        result["targetValueTwo"] = 0.0
        result["zoneNumber"] = int(hr_zone)
    elif target_pace:
        result["targetType"] = {
            "workoutTargetTypeId": 5, "workoutTargetTypeKey": "speed.zone", "displayOrder": 4,
        }
        result["targetValueOne"] = float(target_pace)
        result["targetValueTwo"] = 0.0
    else:
        result["targetType"] = {
            "workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target", "displayOrder": 1,
        }
        result["targetValueTwo"] = 0.0

    if step.get("name"):
        result["stepName"] = str(step["name"])
    return result

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
                rep_step: dict[str, Any] = {"type": "interval"}
                if unit.startswith("min"):
                    rep_step["duration_min"] = value
                elif unit == "km":
                    rep_step["distance_km"] = value
                else:
                    rep_step["distance_m"] = value
                if zone:
                    rep_step["target_hr_zone"] = zone
                steps.append(rep_step)
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
