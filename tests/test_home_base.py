import pytest

import server


def _activity(activity_id, lat, lon, date="2026-11-20T18:00:00.0", activity_type="running"):
    return {
        "activityId": activity_id,
        "startLatitude": lat,
        "startLongitude": lon,
        "startTimeLocal": date,
        "activityType": {"typeKey": activity_type},
    }


class _FakeHomeApi:
    def __init__(self, activities):
        self.activities = activities
        self.calls = []

    def get_activities(self, start, limit):
        self.calls.append((start, limit))
        return self.activities


@pytest.fixture
def home_api(monkeypatch):
    activities = [
        _activity(1, 40.4168, -3.7038),
        _activity(2, 40.4170, -3.7040),
        _activity(3, 40.4165, -3.7035),
        _activity(4, 42.8460, -2.6850),
        _activity(5, None, None),
    ]
    api = _FakeHomeApi(activities)
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    return api


def test_infer_home_base_picks_the_most_repeated_starting_area(home_api):
    result = server.infer_home_base(days=30, max_activities=50)

    assert result["ok"] is True
    assert result["activities_considered"] == 4
    assert result["home_lat"] == pytest.approx(40.4167666, abs=1e-4)
    assert result["home_lon"] == pytest.approx(-3.7037666, abs=1e-4)
    assert result["cluster_count"] == 2
    assert result["applied"] is False


def test_infer_home_base_can_update_the_user_profile(home_api, monkeypatch):
    updates = []
    monkeypatch.setattr(
        server,
        "_update_user",
        lambda user_id, **fields: updates.append((user_id, fields)) or fields,
    )

    result = server.infer_home_base(days=30, max_activities=50, apply=True)

    assert result["applied"] is True
    assert len(updates) == 1
    assert updates[0][0] == "tester"
    assert updates[0][1]["home_lat"] == pytest.approx(40.4167666, abs=1e-4)
    assert "home_name" in updates[0][1]


def test_infer_home_base_fails_cleanly_without_geo_data(monkeypatch):
    api = _FakeHomeApi([_activity(1, None, None)])
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)

    with pytest.raises(RuntimeError, match="con coordenadas de salida"):
        server.infer_home_base(days=30)
