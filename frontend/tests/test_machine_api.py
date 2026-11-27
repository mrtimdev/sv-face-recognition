"""The face verifier SV HRM calls, and the mobile app's API, on the web dashboard."""
import asyncio
from dataclasses import replace
from pathlib import Path

import cv2
import httpx
import numpy as np

from face_attendance import hrm_directory, hrm_sync
from face_attendance.settings import Settings
from face_attendance.web import machine_api, server

from tests.test_web_dashboard import settings_file

VERIFIER_KEY = "test-only-shared-verifier-secret-123456"
MOBILE_KEY = "test-only-mobile-app-secret-1234567890"
MOBILE = {"X-API-KEY": MOBILE_KEY}
SAMPLE = Path(__file__).parents[1] / "face_attendance/assets/samples/live.jpg"


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dashboard.test")


def test_verifier_encodes_faces_with_the_live_monitor_models(tmp_path, monkeypatch):
    app = server.create_app(settings_file(tmp_path, monkeypatch), verifier_key=VERIFIER_KEY)

    async def check():
        async with app.router.lifespan_context(app), client(app) as http:
            headers = {"X-Face-Verifier-Key": VERIFIER_KEY}
            image = SAMPLE.read_bytes()
            assert (await http.post("/v1/faces/encode", content=image)).status_code == 401
            # A key header, not the browser's X-SV-Face header, guards this API.
            response = await http.post("/v1/faces/encode", content=image, headers=headers)
            assert response.status_code == 200
            assert response.json()["model"] == "opencv-sface-2021dec-v1"
            vectors = response.json()["vectors"]
            assert len(vectors) == 1 and len(vectors[0]) == 128
            assert abs(np.linalg.norm(vectors[0]) - 1) < 0.001
            blank = cv2.imencode(".jpg", np.zeros((100, 100, 3), dtype=np.uint8))[1].tobytes()
            assert (await http.post("/v1/faces/encode", content=blank, headers=headers)).json()["vectors"] == []
            assert (await http.post("/v1/faces/encode", content=b"broken", headers=headers)).status_code == 422
            too_big = b"x" * (machine_api.MAX_IMAGE_BYTES + 1)
            assert (await http.post("/v1/faces/encode", content=too_big, headers=headers)).status_code == 413

    asyncio.run(check())


def test_short_keys_switch_the_machine_apis_off(tmp_path, monkeypatch):
    app = server.create_app(settings_file(tmp_path, monkeypatch), verifier_key="short", mobile_key="")

    async def check():
        async with app.router.lifespan_context(app), client(app) as http:
            response = await http.post("/v1/faces/encode", content=b"x", headers={"X-Face-Verifier-Key": "short"})
            assert response.status_code == 503
            assert (await http.get("/api/mobile/employees", headers={"X-API-KEY": ""})).status_code == 503
            # The browser API keeps its own CSRF header check.
            assert (await http.post("/api/engine/start")).status_code == 403

    asyncio.run(check())


def test_mobile_app_reads_employees_from_the_dashboard_and_relays_angles(tmp_path, monkeypatch):
    path = settings_file(tmp_path, monkeypatch)
    settings = replace(Settings.load(path), hrm_api_key="dashboard-only-hrm-key")
    settings.save(path)
    monkeypatch.setattr(hrm_directory, "sync", lambda *args, **kwargs: None)
    rows = [{"employee_id": "SV002", "employee_name": "Bopha Chan", "first_name": "Bopha", "last_name": "Chan",
             "department": "Finance", "hrm_status": "ACTIVE", "has_photo": True, "face_enrolled": False},
            {"employee_id": "SV001", "employee_name": "Dara Sok", "first_name": "Dara", "last_name": "Sok",
             "department": "IT", "hrm_status": "ACTIVE", "has_photo": False, "face_enrolled": True}]
    hrm_directory._write_directory(hrm_directory.cache_path(settings),
                                   hrm_directory.Directory(tuple(rows), 1700000000.0, "http://127.0.0.1"))
    calls = []

    def fake_request(config, method, path, body=None, content_type=None, raw_bytes=False, **_kwargs):
        calls.append((method, path, content_type, body, config.api_key))
        if path.endswith("/BAD/face-angles"):
            raise hrm_sync.HRMError("HRM has no such employee code, or the address is wrong.", 404)
        if raw_bytes:
            return b"\xff\xd8jpeg\xff\xd9"
        return {"success": True, "data": {"path": path}}

    monkeypatch.setattr(hrm_sync, "_request", fake_request)
    app = server.create_app(path, mobile_key=MOBILE_KEY)

    async def check():
        async with app.router.lifespan_context(app), client(app) as http:
            assert (await http.get("/api/mobile/employees")).status_code == 401
            response = await http.get("/api/mobile/employees", headers=MOBILE)
            data = response.json()["data"]
            assert [row["employeeCode"] for row in data["employees"]] == ["SV001", "SV002"]
            assert data["employees"][0] == {"employeeCode": "SV001", "employeeName": "Dara Sok",
                                            "firstName": "Dara", "lastName": "Sok", "departmentName": "IT",
                                            "positionName": "", "hasPhoto": False, "faceEnrolled": True}
            assert data["totalElements"] == 2 and data["totalPages"] == 1
            search = await http.get("/api/mobile/employees", params={"search": "bopha"}, headers=MOBILE)
            assert [row["employeeCode"] for row in search.json()["data"]["employees"]] == ["SV002"]
            paged = await http.get("/api/mobile/employees", params={"page": 1, "size": 1}, headers=MOBILE)
            assert [row["employeeCode"] for row in paged.json()["data"]["employees"]] == ["SV002"]

            assert (await http.get("/api/mobile/face-angles/angles", headers=MOBILE)).json()["success"]
            assert (await http.get("/api/mobile/employees/SV001/face-angles", headers=MOBILE)).status_code == 200
            missing = await http.get("/api/mobile/employees/BAD/face-angles", headers=MOBILE)
            assert missing.status_code == 404 and "no such employee" in missing.json()["message"]
            image = await http.get("/api/mobile/employees/SV001/face-angles/straight/image", headers=MOBILE)
            assert image.headers["content-type"] == "image/jpeg" and image.content.startswith(b"\xff\xd8")
            bad = await http.get("/api/mobile/employees/SV001/face-angles/../image", headers=MOBILE)
            assert bad.status_code in (404, 422)

            upload = await http.post("/api/mobile/employees/SV 1/face-angles/left", headers=MOBILE,
                                     files={"image": ("left.jpg", b"\xff\xd8jpeg\xff\xd9", "image/jpeg")},
                                     data={"source": "MOBILE_CAPTURE"})
            assert upload.status_code == 200
            # The angle worker also talks to HRM in the background; find the relayed upload.
            method, sent_path, content_type, body, key = next(call for call in calls if call[0] == "POST")
            assert (method, sent_path) == ("POST", "/employees/SV%201/face-angles/left")
            assert content_type.startswith("multipart/form-data") and b"MOBILE_CAPTURE" in body
            assert key == "dashboard-only-hrm-key"   # The phone never needs the HRM key.
            wrong_type = await http.post("/api/mobile/employees/SV001/face-angles/left", headers=MOBILE,
                                         content=b"x")
            assert wrong_type.status_code == 415

    asyncio.run(check())
