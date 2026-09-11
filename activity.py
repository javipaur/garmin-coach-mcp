from __future__ import annotations

from typing import Any

from localization import _ACTIVITY_FAMILY_ES, _ACTIVITY_TYPE_ES

_ACTIVITY_TRANSPORT_TYPES = {"motorcycling", "driving", "car", "automotive"}
_ACTIVITY_ENDURANCE_TYPES = {
    "running",
    "treadmill_running",
    "walking",
    "hiking",
    "trail_running",
    "track_running",
}
_ACTIVITY_STRENGTH_TYPES = {"strength_training"}
_ACTIVITY_CYCLING_TYPES = {
    "cycling",
    "indoor_cycling",
    "mountain_biking",
    "road_biking",
    "virtual_ride",
}
_ACTIVITY_SWIM_TYPES = {"lap_swimming", "open_water_swimming", "swimming"}

_ACTIVITY_SUMMARY_KEYS = [
    "distance",
    "duration",
    "elapsedDuration",
    "movingDuration",
    "calories",
    "activityTrainingLoad",
    "trainingEffect",
    "anaerobicTrainingEffect",
    "trainingEffectLabel",
    "aerobicTrainingEffectMessage",
    "anaerobicTrainingEffectMessage",
    "averageHR",
    "maxHR",
    "minHR",
    "averageSpeed",
    "averageMovingSpeed",
    "maxSpeed",
    "avgGradeAdjustedSpeed",
    "averagePower",
    "maxPower",
    "minPower",
    "normalizedPower",
    "totalWork",
    "averageRunCadence",
    "maxRunCadence",
    "groundContactTime",
    "verticalOscillation",
    "verticalRatio",
    "strideLength",
    "steps",
    "averageTemperature",
    "maxTemperature",
    "minTemperature",
    "avgElevation",
    "maxElevation",
    "minElevation",
    "elevationGain",
    "elevationLoss",
    "beginPotentialStamina",
    "endPotentialStamina",
    "minAvailableStamina",
    "differenceBodyBattery",
    "waterEstimated",
    "moderateIntensityMinutes",
    "vigorousIntensityMinutes",
]


def _activity_family(activity_type: str | None) -> str:
    if not activity_type:
        return "other"
    if activity_type in _ACTIVITY_ENDURANCE_TYPES:
        return "endurance"
    if activity_type in _ACTIVITY_STRENGTH_TYPES:
        return "strength"
    if activity_type in _ACTIVITY_CYCLING_TYPES:
        return "cycling"
    if activity_type in _ACTIVITY_SWIM_TYPES:
        return "swimming"
    if activity_type in _ACTIVITY_TRANSPORT_TYPES:
        return "transport"
    return activity_type


def _normalize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    activity_type = activity.get("activityType") or activity.get("activityTypeDTO") or {}
    summary = activity.get("summaryDTO") or {}
    type_key = activity_type.get("typeKey")

    duration_seconds = activity.get("duration")
    if duration_seconds is None:
        duration_seconds = summary.get("duration")

    distance_m = activity.get("distance")
    if distance_m is None:
        distance_m = summary.get("distance")

    return {
        "activity_id": activity.get("activityId"),
        "name": activity.get("activityName"),
        "type": type_key,
        "activity_family": _activity_family(type_key),
        "start_time_local": activity.get("startTimeLocal") or summary.get("startTimeLocal"),
        "duration_min": round((duration_seconds or 0) / 60, 1),
        "distance_km": round((distance_m or 0) / 1000, 2),
        "avg_hr": activity.get("averageHR") or summary.get("averageHR"),
        "max_hr": activity.get("maxHR") or summary.get("maxHR"),
        "calories": activity.get("calories") or summary.get("calories"),
        "training_load": activity.get("trainingLoad")
        or activity.get("activityTrainingLoad")
        or summary.get("activityTrainingLoad"),
        "elevation_gain_m": activity.get("elevationGain") or summary.get("elevationGain"),
        "training_effect": summary.get("trainingEffect"),
        "anaerobic_training_effect": summary.get("anaerobicTrainingEffect"),
        "average_power": activity.get("averagePower") or summary.get("averagePower"),
        "normalized_power": summary.get("normalizedPower"),
        "average_run_cadence": activity.get("averageRunCadence")
        or summary.get("averageRunCadence"),
        "steps": activity.get("steps") or summary.get("steps"),
        "tipo_actividad": _ACTIVITY_TYPE_ES.get(type_key, type_key)
        if isinstance(type_key, str)
        else type_key,
        "familia_actividad": _ACTIVITY_FAMILY_ES.get(
            _activity_family(type_key), _activity_family(type_key)
        ),
    }


def _compact_activity_for_history(activity: dict[str, Any]) -> dict[str, Any]:
    """Lightweight activity dict for history listings."""
    return {
        "activity_id": activity.get("activity_id"),
        "name": activity.get("name"),
        "type": activity.get("type"),
        "tipo_actividad": activity.get("tipo_actividad"),
        "familia_actividad": activity.get("familia_actividad"),
        "activity_family": activity.get("activity_family"),
        "start_time_local": activity.get("start_time_local"),
        "duration_min": activity.get("duration_min"),
        "distance_km": activity.get("distance_km"),
        "avg_hr": activity.get("avg_hr"),
        "calories": activity.get("calories"),
        "training_load": activity.get("training_load"),
    }


def _pick_activity_summary(summary: Any) -> dict[str, Any]:
    if not isinstance(summary, dict):
        return {}
    return {key: summary.get(key) for key in _ACTIVITY_SUMMARY_KEYS if summary.get(key) is not None}


def _pick_activity_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    keys = [
        "distance",
        "duration",
        "elapsedDuration",
        "movingDuration",
        "calories",
        "activityTrainingLoad",
        "trainingEffect",
        "anaerobicTrainingEffect",
        "averageHR",
        "maxHR",
        "averageSpeed",
        "maxSpeed",
        "elevationGain",
        "elevationLoss",
        "averagePower",
        "normalizedPower",
    ]
    return {k: metadata.get(k) for k in keys if metadata.get(k) is not None}


def _extract_primary_device_info(training_status: Any, devices_raw: Any) -> dict[str, Any]:
    device_id = None
    device_name = None
    image_url = None

    if isinstance(training_status, dict):
        try:
            latest = training_status["mostRecentTrainingStatus"]["latestTrainingStatusData"]
            if isinstance(latest, dict) and latest:
                key = next(iter(latest.keys()))
                device_id = int(key)
        except Exception:
            pass

        if device_id is None:
            try:
                balance = training_status["mostRecentTrainingLoadBalance"][
                    "metricsTrainingLoadBalanceDTOMap"
                ]
                if isinstance(balance, dict) and balance:
                    key = next(iter(balance.keys()))
                    device_id = int(key)
            except Exception:
                pass

        for path in [
            ("mostRecentTrainingStatus", "recordedDevices"),
            ("mostRecentTrainingLoadBalance", "recordedDevices"),
        ]:
            try:
                devices = training_status[path[0]][path[1]]
                if isinstance(devices, list):
                    for dev in devices:
                        if not isinstance(dev, dict):
                            continue
                        dev_id = dev.get("deviceId")
                        if device_id is None and dev_id is not None:
                            device_id = dev_id
                        if device_id is not None and dev_id == device_id:
                            device_name = dev.get("deviceName")
                            image_url = dev.get("imageURL")
                            break
                    if device_name:
                        break
            except Exception:
                pass

    if device_id is None and isinstance(devices_raw, list):
        for dev in devices_raw:
            if not isinstance(dev, dict):
                continue
            for key in ("deviceId", "id", "unitId"):
                if dev.get(key) is not None:
                    device_id = dev.get(key)
                    break
            if device_id is not None:
                device_name = (
                    dev.get("deviceName") or dev.get("displayName") or dev.get("modelName")
                )
                image_url = dev.get("imageURL")
                break

    return {
        "primary_device_id": device_id,
        "primary_device_name": device_name,
        "primary_device_image_url": image_url,
    }
