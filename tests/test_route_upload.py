import pytest

import server


class _FakeClient:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def post(self, domain, path, **kwargs):
        self.calls.append((domain, path, kwargs))
        if path.endswith("/import"):
            return self.parsed
        return {"courseId": 999, "courseName": kwargs["json"]["courseName"]}


class _FakeCourseApi:
    def __init__(self, parsed=None):
        self.client = _FakeClient(
            parsed
            or {
                "courseId": None,
                "courseName": "Ruta importada",
                "geoPoints": [
                    {"latitude": 40.0, "longitude": -3.0, "elevation": None},
                    {"latitude": 40.001, "longitude": -3.001, "elevation": None},
                ],
                "coursePoints": [],
            }
        )
        self.upload_activity_calls = []

    def upload_activity(self, path):
        self.upload_activity_calls.append(path)
        raise AssertionError("upload_activity no debe usarse para subir courses")


@pytest.fixture
def course_api(monkeypatch):
    api = _FakeCourseApi()
    monkeypatch.setattr(server, "_get_auth_user", lambda: {"id": "tester"})
    monkeypatch.setattr(server, "_get_api", lambda _user_id=None: api)
    return api


def test_upload_route_to_garmin_imports_and_saves_a_course(course_api):
    route = {
        "name": "Ruta test",
        "points": [{"lat": 40.0, "lon": -3.0}, {"lat": 40.001, "lon": -3.001}],
    }

    result = server.upload_route_to_garmin(route, name="Ruta guardada", description="Test")

    assert result == {"ok": True, "course_id": 999, "name": "Ruta guardada"}
    assert course_api.upload_activity_calls == []
    assert [c[1] for c in course_api.client.calls] == [
        "course-service/course/import",
        "course-service/course",
    ]
    import_kwargs = course_api.client.calls[0][2]
    assert import_kwargs["files"]["file"][0].endswith(".gpx")
    assert import_kwargs["api"] is True
    create_kwargs = course_api.client.calls[1][2]
    assert create_kwargs["json"]["courseName"] == "Ruta guardada"
    assert create_kwargs["json"]["geoPoints"][0]["latitude"] == 40.0
    assert create_kwargs["json"]["sourceTypeId"] == 3


def test_upload_route_to_garmin_rejects_route_without_points(course_api):
    with pytest.raises(RuntimeError, match="no tiene puntos GPS"):
        server.upload_route_to_garmin({"points": []}, name="Ruta vacía")

    assert course_api.client.calls == []


def test_get_course_list_reads_the_course_owner_endpoint(course_api):
    calls = []

    def fake_connectapi(path, **kwargs):
        calls.append((path, kwargs))
        return {"coursesForUser": [{"courseId": 999, "courseName": "Ruta guardada"}]}

    course_api.connectapi = fake_connectapi

    result = server.get_course_list()

    assert result["courses"] == [{"courseId": 999, "courseName": "Ruta guardada"}]
    assert calls == [(("web-gateway/course/owner/"), {})]
