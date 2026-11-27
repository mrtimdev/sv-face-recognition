"""Phones on the same Wi-Fi find the desktop app and pair with a code, without typing a key."""
from dataclasses import replace

import httpx

from face_attendance import hrm_directory
from face_attendance.dashboard.machine_server import MachineServer, ensure_phone_link, new_phone_key
from face_attendance.settings import Settings
from face_attendance.web.machine_api import PAIRING_ATTEMPTS, PAIRING_SECONDS, PhonePairing

from tests.test_machine_server import FakeAngles, FakeEncoder, free_port, wait_until
from tests.test_web_dashboard import settings_file

KEY = "k" * 43


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def pairing(clock=None):
    return PhonePairing(lambda: ("desk1", "Front Desk", KEY), clock=clock or Clock())


def test_the_right_code_gives_the_key_once():
    link = pairing()
    code = link.start()
    assert len(code) == 6 and code.isdigit()
    assert link.claim(code, "Dara's iPhone") == KEY
    assert link.paired_devices == ["Dara's iPhone"]
    assert link.claim(code) is None            # Used up.
    assert not link.active


def test_wrong_codes_end_the_code_so_it_cannot_be_guessed():
    link = pairing()
    code = link.start()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(PAIRING_ATTEMPTS):
        assert link.claim(wrong) is None
    assert not link.active
    assert link.claim(code) is None


def test_a_code_expires():
    clock = Clock()
    link = pairing(clock)
    code = link.start()
    assert link.seconds_left() == PAIRING_SECONDS
    clock.now += PAIRING_SECONDS
    assert link.claim(code) is None
    assert not link.active


def test_the_desktop_makes_its_own_key_and_id_once(tmp_path, monkeypatch):
    settings = Settings.load(settings_file(tmp_path, monkeypatch))
    settings.mobile_link_key = settings.mobile_link_id = ""
    assert ensure_phone_link(settings)
    key, desktop_id = settings.mobile_link_key, settings.mobile_link_id
    assert len(key) >= 32 and len(desktop_id) == 16
    assert not ensure_phone_link(settings)
    new_phone_key(settings)
    assert settings.mobile_link_key != key and settings.mobile_link_id == desktop_id


def test_a_phone_finds_pairs_and_then_uses_the_desktop(tmp_path, monkeypatch):
    monkeypatch.delenv("SV_FACE_MOBILE_KEY", raising=False)
    monkeypatch.delenv("SV_FACE_VERIFIER_KEY", raising=False)
    settings = replace(Settings.load(settings_file(tmp_path, monkeypatch)), hrm_api_key="hrm-key",
                       mobile_link_enabled=True, mobile_link_key=KEY, mobile_link_id="desk1")
    rows = [{"employee_id": "SV001", "employee_name": "Dara Sok", "hrm_status": "ACTIVE"}]
    hrm_directory._write_directory(hrm_directory.cache_path(settings),
                                   hrm_directory.Directory(tuple(rows), 1700000000.0, "http://127.0.0.1"))
    current = {"settings": settings}
    port = free_port()
    machine = MachineServer(lambda: current["settings"], FakeAngles(), host="127.0.0.1", port=port,
                            encoder=FakeEncoder())
    assert machine.enabled
    assert machine.start()
    try:
        assert wait_until(lambda: machine.running)
        base = f"http://127.0.0.1:{port}/api/mobile"
        hello = httpx.get(base + "/link/hello").json()["data"]
        assert hello["service"] == "sv-face" and hello["id"] == "desk1" and not hello["pairing"]
        assert "key" not in hello

        # No code shown on the computer yet: nothing to claim.
        assert httpx.post(base + "/link/pair", json={"code": "123456"}).status_code == 409
        code = machine.pairing.start()
        assert httpx.get(base + "/link/hello").json()["data"]["pairing"]
        assert httpx.post(base + "/link/pair", json={"code": "999999" if code != "999999" else "111111"}
                          ).status_code == 403
        paired = httpx.post(base + "/link/pair", json={"code": code, "device": "Pixel 8"}).json()["data"]
        assert paired == {"key": KEY, "id": "desk1", "name": hello["name"]}

        headers = {"X-API-KEY": paired["key"]}
        employees = httpx.get(base + "/employees", headers=headers)
        assert [row["employeeCode"] for row in employees.json()["data"]["employees"]] == ["SV001"]

        # Disconnect all phones: the old key stops working at once, without a restart.
        current["settings"] = replace(settings, mobile_link_key="n" * 43)
        assert httpx.get(base + "/employees", headers=headers).status_code == 401
        # Switched off in Settings: the API answers 503 instead of serving.
        current["settings"] = replace(settings, mobile_link_enabled=False)
        assert httpx.get(base + "/employees", headers=headers).status_code == 503
        assert httpx.get(base + "/link/hello").status_code == 503
        assert not machine.enabled
    finally:
        machine.stop()


def test_switching_the_link_on_in_settings_starts_the_server(tmp_path, monkeypatch):
    monkeypatch.delenv("SV_FACE_MOBILE_KEY", raising=False)
    monkeypatch.delenv("SV_FACE_VERIFIER_KEY", raising=False)
    settings = replace(Settings.load(settings_file(tmp_path, monkeypatch)),
                       mobile_link_enabled=False, mobile_link_key=KEY, mobile_link_id="desk1")
    current = {"settings": settings}
    machine = MachineServer(lambda: current["settings"], FakeAngles(), host="127.0.0.1",
                            port=free_port(), encoder=FakeEncoder())
    assert not machine.start()
    current["settings"] = replace(settings, mobile_link_enabled=True)
    try:
        assert machine.reconfigure()
        assert wait_until(lambda: machine.running)
    finally:
        machine.stop()


class _FakeLink:
    """What the Settings section and pairing dialog use from MachineServer."""

    def __init__(self, running=True):
        self.running = running
        self.port = 8080
        self.pairing = PhonePairing(lambda: ("desk1", "Front Desk", KEY))

    def identity(self):
        return "desk1", "Front Desk", KEY

    def phone_address(self):
        return "http://192.168.1.20:8080"


_APP = None


def _settings_screen(tmp_path):
    from types import SimpleNamespace
    from PyQt6.QtWidgets import QApplication
    from face_attendance.dashboard.screens.settings import SettingsScreen
    global _APP
    _APP = QApplication.instance() or QApplication([])    # Kept, or Qt aborts on the next widget.
    settings = Settings()
    settings._path = tmp_path / "settings.json"
    settings.mobile_link_key, settings.mobile_link_id = KEY, "desk1"
    return SettingsScreen(SimpleNamespace(restart=lambda _s: None), settings), settings


def test_settings_shows_where_phones_connect_and_can_disconnect_them(tmp_path):
    from unittest.mock import patch
    from PyQt6.QtWidgets import QMessageBox
    screen, settings = _settings_screen(tmp_path)
    assert screen.phone_address.text() == "Starts with the desktop app."
    screen.phone_link = _FakeLink()
    assert "http://192.168.1.20:8080" in screen.phone_address.text()
    assert screen.phone_pair_button.isEnabled()

    screen.fields["mobile_link_enabled"].setChecked(False)
    assert not screen.phone_pair_button.isEnabled()
    screen.fields["mobile_link_enabled"].setChecked(True)

    screen.phone_link = _FakeLink(running=False)
    assert "Not listening on port 8080" in screen.phone_address.text()

    with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
        screen._disconnect_phones()
    assert settings.mobile_link_key != KEY and len(settings.mobile_link_key) >= 32
    assert (tmp_path / "settings.json").exists()


def test_the_pairing_dialog_shows_a_code_and_notices_the_phone(tmp_path):
    from face_attendance.dashboard.widgets.phone_pairing import PhonePairingDialog
    _settings_screen(tmp_path)      # Makes the QApplication.
    link = _FakeLink()
    dialog = PhonePairingDialog(link)
    code = dialog.code.text().replace(" ", "")
    assert len(code) == 6 and link.pairing.active
    assert link.pairing.claim(code, "Pixel 8") == KEY
    dialog._tick()
    assert "Pixel 8" in dialog.status.text()
    dialog.reject()

    dialog = PhonePairingDialog(link)
    dialog.reject()
    assert not link.pairing.active          # An unused code ends with the dialog.
