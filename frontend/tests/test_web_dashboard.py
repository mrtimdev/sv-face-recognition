"""The browser must enforce the same permissions as the desktop on its own API."""
import asyncio
from dataclasses import replace
import time

import httpx
import numpy as np

from face_attendance import hrm_auth
from face_attendance.dashboard.engine import AttendanceEngine
from face_attendance.settings import Settings
from face_attendance.web import runtime, server


HEADERS = {"X-SV-Face": "1"}


def settings_file(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "LOCK_PATH", tmp_path / "attendance.lock")
    path = tmp_path / "settings.json"
    replace(Settings(), hrm_server_mode="office", hrm_office_url="http://127.0.0.1",
            source="rtsp://operator:private@camera.local/secret?token=private",
            db_path=str(tmp_path / "attendance.db"),
            capture_dir=str(tmp_path / "captures"),
            encodings_path=str(tmp_path / "encodings_sface.pickle"),
            employees_path=str(tmp_path / "employees.json"),
            log_path=str(tmp_path / "attendance_log.csv")).save(path)
    return path


def fake_login(_config, username, password):
    if password != "test-password":
        return None
    roles = ["ROLE_ADMIN"] if username == "admin" else ["ROLE_MANAGER"]
    return hrm_auth._build_user(1, username, username.title(), roles, "server-only-token")


def test_requires_session_permissions_and_csrf_header(tmp_path, monkeypatch):
    app = server.create_app(settings_file(tmp_path, monkeypatch))
    monkeypatch.setattr(hrm_auth, "sign_in", fake_login)

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="https://dashboard.test") as client:
                assert (await client.get("/")).status_code == 200
                assert (await client.get("/api/preview")).status_code == 401
                assert (await client.get("/api/attendance")).status_code == 401
                credentials = {"username": "manager", "password": "test-password"}
                assert (await client.post("/api/session", json=credentials)).status_code == 403
                response = await client.post("/api/session", json=credentials, headers=HEADERS)
                assert response.status_code == 200
                cookie = response.headers["set-cookie"]
                assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=strict"))
                assert "server-only-token" not in response.text + cookie
                state = await client.get("/api/live")
                assert state.json()["source"] == "RTSP camera · camera.local"
                assert state.headers["cache-control"] == "no-store"
                assert (await client.get("/api/attendance")).status_code == 200
                assert (await client.get("/api/attendance.csv")).status_code == 403
                assert (await client.get("/api/employees")).status_code == 403
                assert (await client.get("/api/settings")).status_code == 403
                assert (await client.post("/api/engine/start", headers=HEADERS)).status_code == 403
                assert (await client.delete("/api/session", headers=HEADERS)).status_code == 204
                assert (await client.get("/api/live")).status_code == 401

    asyncio.run(check())


def test_admin_engine_preview_settings_and_export(tmp_path, monkeypatch):
    path = settings_file(tmp_path, monkeypatch)
    app = server.create_app(path)
    monkeypatch.setattr(hrm_auth, "sign_in", fake_login)

    class Capture:
        def isOpened(self):
            return True

        def read(self):
            time.sleep(0.025)
            return True, np.full((240, 320, 3), 80, dtype=np.uint8)

        def release(self):
            pass

    def engine(*args, **kwargs):
        return AttendanceEngine(*args, capture_factory=Capture, **kwargs)

    monkeypatch.setattr(runtime, "AttendanceEngine", engine)

    async def check():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://dashboard.test") as client:
                response = await client.post("/api/session", headers=HEADERS,
                                             json={"username": "admin", "password": "test-password"})
                assert response.status_code == 200
                assert (await client.get("/api/employees")).json()["rows"] == []
                response = await client.get("/api/attendance.csv")
                assert response.status_code == 200
                assert response.content.startswith(b"\xef\xbb\xbfRecorded at")
                settings = {"attendance_mode": "smile"}
                response = await client.put("/api/settings", headers=HEADERS, json=settings)
                assert response.status_code == 200
                assert Settings.load(path).attendance_mode == "smile"
                assert ":private@" in Settings.load(path).source
                assert (await client.post("/api/engine/start", headers=HEADERS)).status_code == 204
                assert (await client.put("/api/settings", headers=HEADERS, json=settings)).status_code == 409
                preview = await client.get("/api/preview")
                for _ in range(60):
                    if preview.status_code == 200:
                        break
                    await asyncio.sleep(0.05)
                    preview = await client.get("/api/preview")
                assert preview.headers["content-type"] == "image/jpeg"
                assert preview.content.startswith(b"\xff\xd8")
                assert (await client.post("/api/engine/pause", headers=HEADERS)).status_code == 204
                assert (await client.get("/api/live")).json()["paused"]
                assert (await client.post("/api/engine/resume", headers=HEADERS)).status_code == 204
                assert not (await client.get("/api/live")).json()["paused"]
                assert (await client.post("/api/engine/stop", headers=HEADERS)).status_code == 204
                assert (await client.get("/api/preview")).status_code == 204

    asyncio.run(check())
