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


def _parse_workout_steps_text(desc: str) -> list[dict[str, Any]]:
    """Parse natural language workout description into structured steps."""
    steps = []
    lines = [line.strip() for line in desc.strip().splitlines() if line.strip()]

    for i, line in enumerate(lines):
        step: dict[str, Any] = {"order": i + 1}

        line_lower = line.lower()

        if any(w in line_lower for w in ["calentamiento", "warmup", "warm-up"]):
            step["type"] = "warmup"
        elif any(w in line_lower for w in ["vuelta a la calma", "cooldown", "cool-down"]):
            step["type"] = "cooldown"
        elif any(w in line_lower for w in ["descanso", "recovery", "recuperación", "recuperacion"]):
            step["type"] = "rest"
        else:
            step["type"] = "active"

        duration_match = re.search(r'(\d+)\s*(?:min|minutes?|minutos?)', line_lower)
        if duration_match:
            step["duration_min"] = int(duration_match.group(1))
            step["duration_sec"] = step["duration_min"] * 60

        dist_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:km|kilómetros?|kilometers?)', line_lower)
        if dist_match:
            step["distance_km"] = float(dist_match.group(1))
            step["distance_m"] = step["distance_km"] * 1000

        hr_match = re.search(r'(?:z(?:ona)?|hr|fc)\s*(\d)', line_lower)
        if hr_match:
            step["target_hr_zone"] = int(hr_match.group(1))

        step["name"] = line
        steps.append(step)

    return steps


def _parse_distance_to_m(text: str) -> float | None:
    if not text:
        return None
    text = text.strip().lower()
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:km|kilómetros?)', text)
    if m:
        return float(m.group(1)) * 1000
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:m|metros?|meters?)', text)
    if m:
        return float(m.group(1))
    return None


def _parse_duration_min(text: str) -> float | None:
    if not text:
        return None
    text = text.strip().lower()
    m = re.search(r'(\d+)\s*(?:h|hours?|horas?)', text)
    if m:
        return float(m.group(1)) * 60
    m = re.search(r'(\d+)\s*(?:min|minutes?|minutos?)', text)
    if m:
        return float(m.group(1))
    return None
