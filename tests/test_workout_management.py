import pytest

import server


class _FakeDeleteApi:
    def __init__(self):
        self.deleted = []

    def delete_workout(self, workout_id):
        self.deleted.append(workout_id)
        return {"ok": True}


@pytest.fixture
def fake_api(monkeypatch):
    api = _FakeDeleteApi()
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    return api


def test_delete_workout_removes_the_requested_workout(fake_api):
    result = server.delete_workout("1768681246")

    assert result == {"ok": True, "workout_id": "1768681246", "deleted": 1}
    assert fake_api.deleted == ["1768681246"]


def test_delete_workout_rejects_a_non_numeric_id(fake_api):
    with pytest.raises(ValueError, match="ID de entrenamiento no válido"):
        server.delete_workout("no-es-un-id")

    assert fake_api.deleted == []


def test_delete_workouts_dry_run_leaves_library_untouched(fake_api):
    result = server.delete_workouts(["111", "222"], dry_run=True)

    assert result["dry_run"] is True
    assert result["deleted"] == 0
    assert result["workouts"] == ["111", "222"]
    assert fake_api.deleted == []


def test_delete_workouts_deletes_every_id_when_confirmed(fake_api):
    result = server.delete_workouts(["111", "222"], dry_run=False)

    assert result["dry_run"] is False
    assert result["deleted"] == 2
    assert fake_api.deleted == ["111", "222"]
