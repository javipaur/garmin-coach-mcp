from datetime import date

from date_utils import _format_duration_hm, _gsec_to_text, _isoish_to_local, _parse_date


def test_format_duration_hm_basic():
    assert _format_duration_hm(3661) == "1h 01m"


def test_format_duration_hm_zero():
    assert _format_duration_hm(0) == "0h 00m"


def test_format_duration_hm_none():
    assert _format_duration_hm(None) is None


def test_format_duration_hm_negative():
    assert _format_duration_hm(-5) is None


def test_format_duration_hm_invalid():
    assert _format_duration_hm("abc") is None


def test_gsec_to_text_hours():
    assert _gsec_to_text(3661) == "1h 01m 01s"


def test_gsec_to_text_minutes():
    assert _gsec_to_text(65) == "1m 05s"


def test_gsec_to_text_seconds():
    assert _gsec_to_text(5) == "5s"


def test_gsec_to_text_none():
    assert _gsec_to_text(None) is None


def test_parse_date_iso():
    assert _parse_date("2026-09-11") == date(2026, 9, 11)


def test_parse_date_dmy():
    assert _parse_date("11/09/2026") == date(2026, 9, 11)


def test_parse_date_date_object():
    d = date(2026, 9, 11)
    assert _parse_date(d) is d


def test_parse_date_invalid():
    assert _parse_date("no es fecha") is None


def test_parse_date_none():
    assert _parse_date(None) is None


def test_isoish_to_local_utc():
    out = _isoish_to_local("2026-09-11T07:00:18Z")
    assert out is not None
    assert out.endswith("+02:00")


def test_isoish_to_local_none():
    assert _isoish_to_local(None) is None


def test_isoish_to_local_invalid_keeps_value():
    assert _isoish_to_local("garbage") == "garbage"
