"""The desktop dashboard serves the same machine APIs as the web dashboard."""
import socket
import time
from dataclasses import replace

import httpx

from face_attendance import hrm_directory
from face_attendance.dashboard.machine_server import MachineServer
from face_attendance.settings import Settings

from tests.test_machine_api import MOBILE, MOBILE_KEY, VERIFIER_KEY
from tests.test_web_dashboard import settings_file


class FakeEncoder:
    def encode(self, content):
        return {"model": "test", "vectors": [[1.0]]}


class FakeAngles:
    def __init__(self):
        self.requests = 0

    def request(self):
        self.requests += 1


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(check, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def test_desktop_serves_verifier_and_mobile_employees(tmp_path, monkeypatch):
    settings = replace(Settings.load(settings_file(tmp_path, monkeypatch)), hrm_api_key="hrm-key")
    rows = [{"employee_id": "SV001", "employee_name": "Dara Sok", "hrm_status": "ACTIVE"}]
    hrm_directory._write_directory(hrm_directory.cache_path(settings),
                                   hrm_directory.Directory(tuple(rows), 1700000000.0, "http://127.0.0.1"))
    port = free_port()
    machine = MachineServer(lambda: settings, FakeAngles(), verifier_key=VERIFIER_KEY,
                            mobile_key=MOBILE_KEY, host="127.0.0.1", port=port, encoder=FakeEncoder())
    assert machine.start()
    try:
        assert wait_until(lambda: machine.running)
        base = f"http://127.0.0.1:{port}"
        assert httpx.post(base + "/v1/faces/encode", content=b"jpg").status_code == 401
        encoded = httpx.post(base + "/v1/faces/encode", content=b"jpg",
                             headers={"X-Face-Verifier-Key": VERIFIER_KEY})
        assert encoded.status_code == 200 and encoded.json()["vectors"] == [[1.0]]
        employees = httpx.get(base + "/api/mobile/employees", headers=MOBILE)
        assert employees.status_code == 200
        assert [row["employeeCode"] for row in employees.json()["data"]["employees"]] == ["SV001"]
    finally:
        machine.stop()
    assert not machine.running


def test_desktop_machine_apis_stay_off_without_a_key(monkeypatch):
    monkeypatch.delenv("SV_FACE_VERIFIER_KEY", raising=False)
    monkeypatch.delenv("SV_FACE_MOBILE_KEY", raising=False)
    machine = MachineServer(lambda: None, FakeAngles())
    assert not machine.enabled
    assert not machine.start()
    machine.stop()
