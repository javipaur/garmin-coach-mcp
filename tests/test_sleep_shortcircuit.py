from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime

import sleep
from date_utils import _today_local
from sleep import _Garmin_get_sleep_data_multi_day


def _today_iso():
    return _today_local().isoformat()


def _install_fake_garmin(monkeypatch, calls):
    def _fake_get_sleep_data(self, cdate):
        calls.append(cdate)
        return _raw(cdate)

    monkeypatch.setattr(
        sleep, "_Garmin_get_sleep_data_original", _fake_get_sleep_data
    )
    return _fake_get_sleep_data


def _epoch(int_day, hour):
    d = datetime.combine(int_day, dtime(hour=hour), tzinfo=UTC)
    return int(d.timestamp() * 1000)


def _raw(cdate_iso, start_hour=22, end_hour=7):
    start = date.fromisoformat(cdate_iso)
    end = start + timedelta(days=1)

    return {
        "dailySleepDTO": {
            "calendarDate": cdate_iso,
            "sleepTimeSeconds": (end_hour - start_hour + 24) % 24 * 3600,
            "sleepStartTimestampGMT": _epoch(start, start_hour),
            "sleepEndTimestampGMT": _epoch(end, end_hour),
        },
    }


def test_past_date_shortcircuits_after_first_success(monkeypatch):
    calls = []
    _install_fake_garmin(monkeypatch, calls)

    out = _Garmin_get_sleep_data_multi_day(None, "2026-09-10")

    # Para fechas pasadas el primer intento (el propio día) ya es definitivo;
    # no se consultan offsets -1 ni -2.
    assert calls == ["2026-09-10"]
    assert out["dailySleepDTO"]["calendarDate"] == "2026-09-10"


def test_today_still_checks_multiple_offsets(monkeypatch):
    calls = []
    _install_fake_garmin(monkeypatch, calls)

    today = _today_iso()
    out = _Garmin_get_sleep_data_multi_day(None, today)

    # Para hoy el wrapper verifica ayer (-1), hoy (0) y -2 buscando la última
    # noche completada; el orden debe empezar por el día anterior.
    assert calls[0] == (date.fromisoformat(today) - timedelta(days=1)).isoformat()
    assert out["dailySleepDTO"]["calendarDate"] in calls


def test_uses_captured_original_not_class_attribute(monkeypatch):
    # Escenario de producción: server.py reemplaza Garmin.get_sleep_data con el
    # wrapper. El wrapper debe llamar al original capturado al importar, NO leer
    # la clase en cada llamada (eso era la recursión infinita).
    calls = []
    fake_orig = _install_fake_garmin(monkeypatch, calls)

    fake_cls = type("G", (), {"get_sleep_data": _Garmin_get_sleep_data_multi_day})

    # Reemplazo como hace el glue de server.py.
    fake_cls.get_sleep_data = _Garmin_get_sleep_data_multi_day
    monkeypatch.setattr(sleep, "_Garmin_get_sleep_data_original", fake_orig)

    out = _Garmin_get_sleep_data_multi_day(None, "2026-09-10")

    assert fake_cls.get_sleep_data is _Garmin_get_sleep_data_multi_day
    assert calls == ["2026-09-10"]
    assert out["dailySleepDTO"]["calendarDate"] == "2026-09-10"
