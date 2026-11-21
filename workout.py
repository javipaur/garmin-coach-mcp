from __future__ import annotations

import re
from typing import Any


def _garmin_workout_step_from_desc(step: dict[str, Any], step_order: int) -> dict[str, Any]:
    step_type = step.get("type", "active").lower()
    intensity_map = {
        "warmup": "warmup",
        "calentamiento": "warmup",
        "active": "interval",
        "interval": "interval",
        "intervalo": "interval",
        "rest": "recovery",
        "recovery": "recovery",
        "recuperación": "recovery",
        "recuperacion": "recovery",
        "cooldown": "cooldown",
        "vuelta a la calma": "cooldown",
    }
    intensity = intensity_map.get(step_type, "interval")

    step_type_id = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "repeat": 6}.get(
        intensity, 3
    )

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
        "stepType": {
            "stepTypeId": step_type_id,
            "stepTypeKey": intensity,
            "displayOrder": step_type_id,
        },
    }

    if duration_value is not None:
        condition_id = 2 if duration_type == "time" else 3
        result["endCondition"] = {
            "conditionTypeId": condition_id,
            "conditionTypeKey": duration_type,
            "displayOrder": condition_id,
            "displayable": True,
        }
        result["endConditionValue"] = duration_value

    hr_zone = step.get("target_hr_zone")
    pace_bounds = _pace_bounds_from_step(step)
    if hr_zone:
        result["targetType"] = {
            "workoutTargetTypeId": 4,
            "workoutTargetTypeKey": "heart.rate.zone",
            "displayOrder": 3,
        }
        result["targetValueOne"] = 0.0
        result["targetValueTwo"] = 0.0
        result["zoneNumber"] = int(hr_zone)
    elif pace_bounds:
        low, high = pace_bounds
        result["targetType"] = {
            "workoutTargetTypeId": 6,
            "workoutTargetTypeKey": "pace.zone",
            "displayOrder": 1,
        }
        # Garmin Connect guarda el extremo rápido (m/s mayor) en targetValueOne
        # y el lento (m/s menor) en targetValueTwo. Al revés, el servidor descarta
        # ambos valores y el reloj muestra el objetivo como zona por defecto.
        result["targetValueOne"] = high
        result["targetValueTwo"] = low
    else:
        result["targetType"] = {
            "workoutTargetTypeId": 1,
            "workoutTargetTypeKey": "no.target",
            "displayOrder": 1,
        }
        result["targetValueTwo"] = 0.0

    if step.get("name"):
        result["stepName"] = str(step["name"])
    return result


_PACE_UNIT = r"(?:/\s*(km|mi|kilómetros?|kilometros?|millas?|k\b))?"
_PACE_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*(?:[-–—]|entre|desde|de|a|hasta|y|e)\s*"
    r"(\d{1,2}):(\d{2})\s*" + _PACE_UNIT,
    re.I,
)
_PACE_SINGLE_RE = re.compile(r"(\d{1,2}):(\d{2})\s*" + _PACE_UNIT, re.I)


def _pace_text_in(text: str) -> str | None:
    """Devuelve el fragmento de texto que contiene el ritmo (rango o valor único)."""
    m = _PACE_RANGE_RE.search(text)
    if m:
        return m.group(0)
    m = _PACE_SINGLE_RE.search(text)
    return m.group(0) if m else None


def _parse_pace_range(text: str) -> tuple[float, float] | None:
    """Convierte '4:10-4:15/km', 'entre 4:30 y 4:35' o '4:30/km' a (m/s lentos, m/s rápidos)."""
    m = _PACE_RANGE_RE.search(text)
    if m:
        unit = (m.group(5) or "").lower()
        to_meters = 1609.344 if unit.startswith(("mi", "milla")) else 1000.0
        a = to_meters / (int(m.group(1)) * 60 + int(m.group(2)))
        b = to_meters / (int(m.group(3)) * 60 + int(m.group(4)))
        return round(min(a, b), 3), round(max(a, b), 3)
    m = _PACE_SINGLE_RE.search(text)
    if not m:
        return None
    unit = (m.group(3) or "").lower()
    to_meters = 1609.344 if unit.startswith(("mi", "milla")) else 1000.0
    value = to_meters / (int(m.group(1)) * 60 + int(m.group(2)))
    return round(value, 3), round(value, 3)


def _pace_bounds_from_step(step: dict[str, Any]) -> tuple[float, float] | None:
    """Extrae el rango de ritmo en m/s de un paso normalizado."""
    low = step.get("target_pace_low_mps")
    high = step.get("target_pace_high_mps")
    if low is not None and high is not None:
        a, b = float(low), float(high)
        return (a, b) if a <= b else (b, a)
    single = step.get("target_pace_mps")
    if single:
        value = float(single)
        return value, value
    pace_text = step.get("target_pace")
    if isinstance(pace_text, str):
        return _parse_pace_range(pace_text)
    return None


_REPEAT_KEYS = {"repeat", "repetición", "repeticion", "rep", "series", "bloque"}
_NESTED_KEYS = ("steps", "workout_steps", "workoutSteps", "childSteps")


def _garmin_workout_steps_from_desc(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convierte pasos normalizados a pasos Garmin, con bloques repeat reales."""
    result: list[dict[str, Any]] = []
    order = 1
    for step in steps:
        step_type = str(step.get("type", "active")).lower()
        repetitions = step.get(
            "iterations",
            step.get("reps", step.get("numberOfIterations", step.get("repeat"))),
        )
        nested_source = next((step[k] for k in _NESTED_KEYS if step.get(k)), None)
        if step_type in _REPEAT_KEYS or (repetitions and nested_source):
            iterations = int(repetitions or 1)
            nested: list[dict[str, Any]] = []
            for child in nested_source or []:
                nested.append(_garmin_workout_step_from_desc(child, order + 1 + len(nested)))
            group: dict[str, Any] = {
                "type": "RepeatGroupDTO",
                "stepOrder": order,
                "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat", "displayOrder": 6},
                "numberOfIterations": iterations,
                "workoutSteps": nested,
                "endCondition": {
                    "conditionTypeId": 7,
                    "conditionTypeKey": "iterations",
                    "displayOrder": 7,
                    "displayable": False,
                },
                "endConditionValue": float(iterations),
                "smartRepeat": False,
            }
            if step.get("skip_last_rest_step") is not None:
                group["skipLastRestStep"] = bool(step["skip_last_rest_step"])
            result.append(group)
            order += 1 + len(nested)
        else:
            result.append(_garmin_workout_step_from_desc(step, order))
            order += 1
    return result


def _is_rest_only(text: str) -> bool:
    """True si el fragmento describe solo una recuperación entre series."""
    lower = text.lower()
    has_rest = any(w in lower for w in ("rec", "rest", "descanso", "recuperación", "recuperacion"))
    has_main = any(
        w in lower for w in ("calentamiento", "warmup", "calma", "cooldown", "enfriamiento")
    )
    return has_rest and not has_main


def _parse_distance_to_m(text: str) -> float | None:
    """Convierte una distancia textual (km o m) a metros."""
    m_km = re.search(r"(\d+(?:\.\d+)?)\s*(km|kilómetros?|kilometros?|k\b)", text, re.I)
    if m_km:
        return float(m_km.group(1)) * 1000
    m_m = re.search(r"(\d+(?:\.\d+)?)\s*m\b", text, re.I)
    if m_m:
        return float(m_m.group(1))
    return None


def _parse_duration_min(text: str) -> float | None:
    """Convierte una duración textual (min o ' horizonte) a minutos. Solo 'min'/'m' claros."""
    m_min = re.search(r"(\d+(?:\.\d+)?)\s*(?:min|mins|minutes?|minutos?)\b", text, re.I)
    if m_min:
        return float(m_min.group(1))
    return None


def _parse_workout_steps_text(desc: str) -> list[dict[str, Any]]:
    """Convierte una descripción de entrenamiento en una lista de pasos normalizados.

    Entiende calentamiento, series con repeticiones (3x800m, 4x1km, 5x200m),
    descansos, zonas, ritmos por kilómetro y vuelta a la calma. Cada serie se
    conserva como un bloque ``repeat`` con sus pasos anidados.
    """
    parts = [p.strip() for p in re.split(r"[,\n;]+", desc) if p.strip()]
    steps: list[dict[str, Any]] = []
    pending_zone: int | None = None
    pending_repeat: dict[str, Any] | None = None

    for part in parts:
        lower = part.lower()

        # Captura un "Z4" suelto (zona que se aplica al último paso de la serie anterior).
        zone_match = re.fullmatch(r"\s*z\s*(\d)\s*", lower)
        if zone_match and steps:
            pending_zone = int(zone_match.group(1))
            target = pending_repeat["steps"][-1] if pending_repeat else steps[-1]
            target["target_hr_zone"] = pending_zone
            continue

        # ---- Series con repeticiones: 3x800m, 4x1km, 5x200m, 3 x 800 m ----
        series_match = re.search(
            r"(\d+)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*(km|kilómetros?|kilometros?|m\b|min(?:uto?s)?\b)",
            part,
            re.I,
        )
        if series_match:
            reps = int(series_match.group(1))
            value = float(series_match.group(2))
            unit = series_match.group(3).lower()
            # Zona de intensidad de la serie (puede ir en la misma parte: 4x1km Z4)
            zone_m_pre = re.search(r"[Zz]\s*(\d)", part)
            zone = int(zone_m_pre.group(1)) if zone_m_pre else pending_zone
            # Descanso entre repeticiones: "400m rec", "rec 400m", "descanso 2min"
            rest_m = None
            rest_min = None
            m_rest = re.search(
                r"(?:rec|rest|descanso|recuperación)\s*(?:de\s*)?(\d+(?:\.\d+)?)\s*(km|m\b|min(?:uto?s)?\b|s(?:eg(?:undo)?s)?\b)?",
                lower,
            ) or re.search(
                r"(\d+(?:\.\d+)?)\s*(m\b|km|min(?:uto?s)?\b|s(?:eg(?:undo)?s)?\b)?\s*(?:rec|rest|descanso|recuperación)",
                lower,
            )
            if m_rest:
                rest_num = float(m_rest.group(1))
                rest_unit = (m_rest.group(2) or "m").lower()
                if rest_unit.startswith("min"):
                    rest_min = rest_num
                elif rest_unit.startswith(("s", "seg")):
                    rest_min = rest_num / 60
                elif rest_unit == "km":
                    rest_m = rest_num * 1000
                else:
                    rest_m = rest_num

            pace_text = _pace_text_in(part)
            rep_step: dict[str, Any] = {"type": "interval"}
            if unit.startswith("min"):
                rep_step["duration_min"] = value
            elif unit == "km":
                rep_step["distance_km"] = value
            else:
                rep_step["distance_m"] = value
            if pace_text:
                rep_step["target_pace"] = pace_text
            elif zone:
                rep_step["target_hr_zone"] = zone

            group_steps: list[dict[str, Any]] = [rep_step]
            if rest_m or rest_min:
                rest_step: dict[str, Any] = {"type": "rest"}
                if rest_min:
                    rest_step["duration_min"] = rest_min
                else:
                    rest_step["distance_m"] = rest_m
                group_steps.append(rest_step)

            pending_repeat = {"type": "repeat", "iterations": reps, "steps": group_steps}
            steps.append(pending_repeat)
            pending_zone = None
            continue

        # ---- Recuperación suelta tras una serie: va dentro del bloque repeat ----
        if pending_repeat and _is_rest_only(part):
            rest_step = {"type": "rest"}
            rest_duration = _parse_duration_min(part)
            rest_distance = _parse_distance_to_m(part)
            if rest_duration is not None:
                rest_step["duration_min"] = rest_duration
            elif rest_distance is not None:
                if rest_distance >= 1000:
                    rest_step["distance_km"] = rest_distance / 1000.0
                else:
                    rest_step["distance_m"] = rest_distance
            else:
                rest_step["duration_min"] = 30
            pending_repeat["steps"].append(rest_step)
            continue

        # ---- Distancia + zona (5km Z2, 800m) ----
        dist_m = _parse_distance_to_m(part)
        duration_min = _parse_duration_min(part)
        zone_m = re.search(r"[Zz]\s*(\d)", part)

        is_warmup = any(w in lower for w in ("calentamiento", "warmup", "warm up", "calentar"))
        is_cooldown = any(
            w in lower
            for w in (
                "vuelta a la calma",
                "cooldown",
                "cool down",
                "calma",
                "enfriamiento",
                "vuelta al ruedo",
            )
        )
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
        generic_pace = _pace_text_in(part)
        if generic_pace:
            step["target_pace"] = generic_pace
            step.pop("target_hr_zone", None)
        if not consumed:
            # Solo texto sin medida explícita → serie genérica de 30 min
            step["duration_min"] = 30
        steps.append(step)
        pending_repeat = None

    return steps
