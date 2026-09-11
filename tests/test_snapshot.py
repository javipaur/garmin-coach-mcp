import threading

import snapshot


class FakeGarmin:
    def get_user_summary(self, day=None):
        return {"totalSteps": 100, "totalDistanceMeters": 5000}

    def get_stats(self, day=None):
        return {"totalSteps": 100}

    def get_heart_rates(self, day=None):
        return {}

    def get_rhr_day(self, day=None):
        return {}

    def get_sleep_data(self, day=None):
        return {
            "dailySleepDTO": {
                "calendarDate": day or "2026-09-11",
                "sleepTimeSeconds": 7 * 3600,
                "sleepStartTimestampGMT": 0,
                "sleepEndTimestampGMT": 0,
                "sleepStartTimestampLocal": "2026-09-11T22:30:00",
                "sleepEndTimestampLocal": "2026-09-12T06:20:00",
                "awakeDuration": 600,
                "sleepScores": {"wellness": {"overall": 82}},
                "sleepLevels": {
                    "summary": {
                        "deep": {"seconds": 3000},
                        "rem": {"seconds": 4200},
                        "light": {"seconds": 12000},
                    }
                },
            },
            "calendarDate": day or "2026-09-11",
        }

    def get_stress_data(self, day=None):
        return {"stressQualifier": "LOW", "avgStressLevel": 20}

    def get_body_battery(self, day=None):
        return {}

    def get_hrv_data(self, day=None):
        return {}

    def get_max_metrics(self, day=None):
        return {}

    def get_training_readiness(self, day=None):
        return {"score": 65, "readinessLevelEnum": "HIGH"}

    def get_training_status(self, day=None):
        return {}


def make_config(fake):
    snapshot.configure(
        FETCH_LOCK=threading.Lock(),
        ACTIVITY_LIMIT=8,
        _get_api=lambda: fake,
        _optional_call_first=lambda api, methods, *args: (getattr(api, methods[0])(*args), None),
        _collect_extra_raw=lambda api, day, training_status: ({}, {}, {}),
        _resting_hr=lambda heart: 58,
        _extract_vo2=lambda max_metrics, training_status: 48,
        _sleep_metrics=lambda s: {},
        _stress_metrics=lambda s: {},
        _body_battery_metrics=lambda b: {},
        _hrv_metrics=lambda h: {},
        _training_readiness_metrics=lambda tr: {"training_readiness_score": (tr or {}).get("score")},
        _normalize_activity=lambda a: a,
        _select_training_readiness_entry=lambda tr: (tr or {}).get("trainingReadinessSummaries") or (
            tr if isinstance(tr, dict) else None
        ),
        _attach_frontend_view_to_snapshot=lambda snap: ("ATTACHED", snap),
        _get_hrv_debug_last=lambda: None,
    )


def test_snapshot_pipeline_runs_full_chain():
    fake = FakeGarmin()
    make_config(fake)

    result = snapshot._collect_day_snapshot("2026-09-11", include_recent_activities=False)
    marker, snap = result
    assert marker == "ATTACHED"
    assert isinstance(snap, dict)
    assert snap["date"].isoformat() == "2026-09-11"
    assert "metrics" in snap
    assert "raw_sources" in snap
    assert "source_errors" in snap

    metrics = snap["metrics"]
    assert metrics["resting_hr"] == 58
    assert metrics["vo2max"] == 48
    assert metrics["steps"] == 100


def test_snapshot_pipeline_translates_es_fields():
    fake = FakeGarmin()
    make_config(fake)

    _, snap = snapshot._collect_day_snapshot("2026-09-11")
    metrics = snap["metrics"]

    assert metrics.get("stress_label") == "LOW"
    assert metrics.get("stress_label_es") == "Bajo"
    assert metrics.get("training_readiness_score") == 65
    assert metrics.get("training_readiness_status_es") in (None, "Alto")
    assert "duracion_de_sueno_texto" in metrics
    assert "sueno_resumen_humano" in metrics or "sueno_fases_resumen_humano" in metrics


def test_attach_frontend_runs_last():
    fake = FakeGarmin()
    make_config(fake)

    result = snapshot._collect_day_snapshot("2026-09-11")
    assert result[0] == "ATTACHED"