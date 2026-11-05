"""Choosing the SV HRM server: Automatic, Online or Office, on sign-in and in Settings."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from face_attendance import hrm_servers as servers
from face_attendance.hrm_sync import config_from_settings
from face_attendance.settings import Settings

ONLINE = "https://hrm.example.com"
OFFICE = "http://192.168.1.46:8082"
HRM = json.dumps({"app": "svhrm", "status": "UP"}).encode()


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def network(answers):
    """An opener answering by address: bytes are the body, an exception is raised."""
    seen = []

    def opener(request, timeout):
        seen.append(request)
        for url, answer in answers.items():
            if request.full_url.startswith(url):
                if isinstance(answer, Exception):
                    raise answer
                return _Answer(answer)
        raise urllib.error.URLError("unreachable")
    opener.seen = seen
    return opener


def saved(**values):
    return SimpleNamespace(**values)


class AddressTests(unittest.TestCase):
    def test_a_mobile_style_address_means_the_same_server(self):
        for text in (f" {OFFICE}/api ", f"{OFFICE}/api/", f"{OFFICE}/api/face-recognition", f"{OFFICE}/"):
            self.assertEqual(servers.normalize_url(text), OFFICE)

    def test_a_bad_address_is_explained_and_an_empty_one_uses_the_default(self):
        self.assertTrue(servers.address_problem("ftp://hrm.example.com"))
        self.assertTrue(servers.address_problem("hrm.example.com"))
        self.assertEqual(servers.address_problem(""), "")
        self.assertEqual(servers.address_problem(ONLINE), "")


class ProfileTests(unittest.TestCase):
    def test_a_new_install_is_automatic_and_starts_on_the_office_address(self):
        choice = servers.profile(saved())
        self.assertEqual(choice.mode, servers.AUTO)
        self.assertEqual(servers.current_url(saved()), choice.office_url)
        self.assertEqual(choice.online_url, servers.DEFAULT_ONLINE_URL)

    def test_an_address_saved_before_modes_existed_stays_the_only_one_used(self):
        self.assertEqual(servers.profile(saved(hrm_api_url=f"{ONLINE}/")), servers.Profile(servers.ONLINE, ONLINE, ""))
        self.assertEqual(servers.profile(saved(hrm_api_url=OFFICE)), servers.Profile(servers.OFFICE, "", OFFICE))
        self.assertEqual(servers.current_url(saved(hrm_api_url=ONLINE)), ONLINE)

    def test_each_fixed_mode_uses_its_own_address(self):
        for mode, url in ((servers.ONLINE, ONLINE), (servers.OFFICE, OFFICE)):
            settings = saved(hrm_server_mode=mode, hrm_online_url=ONLINE, hrm_office_url=OFFICE,
                             hrm_api_url="https://old.example.com")
            self.assertEqual(config_from_settings(settings).base_url, url)

    def test_an_unknown_saved_mode_falls_back_to_automatic(self):
        self.assertEqual(servers.profile(saved(hrm_server_mode="nonsense")).mode, servers.AUTO)

    def test_a_choice_survives_a_save_and_load(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            settings = Settings()
            settings.hrm_api_url = OFFICE
            settings.update(servers.to_settings(servers.Profile(servers.ONLINE, ONLINE, OFFICE)))
            settings.save(path)
            loaded = Settings.load(path)
        self.assertEqual(servers.profile(loaded), servers.Profile(servers.ONLINE, ONLINE, OFFICE))
        self.assertEqual(loaded.hrm_api_url, "")


class ProbeTests(unittest.TestCase):
    AUTO = servers.Profile(servers.AUTO, ONLINE, OFFICE)

    def test_a_probe_carries_no_credentials(self):
        opener = network({OFFICE: HRM})
        self.assertTrue(servers.probe(servers.OFFICE, OFFICE, opener=opener).ok)
        request = opener.seen[0]
        self.assertEqual(request.full_url, OFFICE + "/api/public/ping")
        names = {name.lower() for name in request.headers}
        self.assertFalse(names & {"x-api-key", "authorization", "cookie"})

    def test_only_svhrm_counts_as_connected(self):
        for answer in (b'{"app":"other"}', b"<html>", b"[]", urllib.error.HTTPError(OFFICE, 404, "", {}, None),
                       TimeoutError(), OSError("boom")):
            connection = servers.probe(servers.OFFICE, OFFICE, opener=network({OFFICE: answer}))
            self.assertFalse(connection.ok, answer)
            self.assertIn("192.168.1.46:8082", connection.detail)

    def test_automatic_prefers_the_office(self):
        connection = servers.resolve(self.AUTO, network({OFFICE: HRM, ONLINE: HRM}))
        self.assertEqual((connection.ok, connection.network), (True, servers.OFFICE))

    def test_automatic_uses_the_internet_away_from_the_office(self):
        opener = network({ONLINE: HRM})
        connection = servers.resolve(self.AUTO, opener)
        self.assertEqual((connection.ok, connection.network), (True, servers.ONLINE))
        settings = saved(hrm_server_mode=servers.AUTO, hrm_online_url=ONLINE, hrm_office_url=OFFICE)
        self.assertEqual(servers.current_url(settings), ONLINE)
        servers.resolve(self.AUTO, network({OFFICE: HRM}))
        self.assertEqual(servers.current_url(settings), OFFICE)

    def test_automatic_keeps_the_last_good_address_when_nothing_answers(self):
        servers.resolve(self.AUTO, network({ONLINE: HRM}))
        connection = servers.resolve(self.AUTO, network({}))
        self.assertFalse(connection.ok)
        settings = saved(hrm_server_mode=servers.AUTO, hrm_online_url=ONLINE, hrm_office_url=OFFICE)
        self.assertEqual(servers.current_url(settings), ONLINE)

    def test_a_fixed_mode_never_tries_the_other_address(self):
        opener = network({OFFICE: HRM})
        connection = servers.resolve(servers.Profile(servers.ONLINE, ONLINE, OFFICE), opener)
        self.assertFalse(connection.ok)
        self.assertEqual([r.full_url for r in opener.seen], [ONLINE + "/api/public/ping"])

    def test_status_reads_like_the_mobile_app(self):
        connection = servers.Connection(servers.OFFICE, OFFICE, True, 433)
        self.assertEqual(servers.status_lines(connection),
                         ("Connected · Office network", "192.168.1.46:8082 · 433 ms"))


def pump(condition, timeout_ms=4000):
    waited = 0
    while not condition() and waited < timeout_ms:
        QTest.qWait(20)
        waited += 20
    return condition()


class _Qt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "settings.json"
        self.settings = Settings()
        self.settings._path = self.path


class SignInFormTests(_Qt):
    def _dialog(self):
        from face_attendance.dashboard.login import UserLoginDialog
        dialog = UserLoginDialog(self.settings)
        self.addCleanup(dialog.close)
        return dialog

    def test_switching_on_the_form_saves_and_keeps_the_addresses(self):
        self.settings.update(servers.to_settings(servers.Profile(servers.AUTO, ONLINE, OFFICE)))
        dialog = self._dialog()
        with patch.object(servers, "resolve", return_value=servers.Connection(servers.ONLINE, ONLINE, True, 5)):
            dialog.server.buttons[servers.ONLINE].click()
            self.assertTrue(pump(lambda: dialog.server.status.text().startswith("Connected")))
        self.assertEqual(config_from_settings(self.settings).base_url, ONLINE)
        self.assertEqual(servers.profile(Settings.load(self.path)), servers.Profile(servers.ONLINE, ONLINE, OFFICE))

    def test_the_form_has_no_address_fields(self):
        dialog = self._dialog()
        self.assertIsNone(dialog.server.online_edit)
        self.assertIsNone(dialog.server.office_edit)

    def test_the_choice_cannot_change_during_a_sign_in(self):
        dialog = self._dialog()
        dialog._set_inputs_enabled(False)
        self.assertFalse(dialog.server.isEnabled())

    def test_sign_in_goes_to_the_chosen_server(self):
        from face_attendance.dashboard import login
        self.settings.update(servers.to_settings(servers.Profile(servers.ONLINE, ONLINE, OFFICE)))
        with patch.object(login.hrm_auth, "sign_in", return_value=None) as sign_in:
            login._SignInWorker(1, self.settings, "a", "b").run()
        self.assertEqual(sign_in.call_args.args[0].base_url, ONLINE)

    def test_automatic_sign_in_finds_the_network_first(self):
        from face_attendance.dashboard import login
        self.settings.update(servers.to_settings(servers.Profile(servers.AUTO, ONLINE, OFFICE)))
        answer = network({ONLINE: HRM})
        with patch.object(servers, "_open", answer), patch.object(login.hrm_auth, "sign_in", return_value=None) as sign_in:
            login._SignInWorker(1, self.settings, "a", "b").run()
        self.assertEqual(sign_in.call_args.args[0].base_url, ONLINE)


class SettingsScreenTests(_Qt):
    def setUp(self):
        super().setUp()
        from face_attendance.dashboard.screens.settings import SettingsScreen
        self.screen = SettingsScreen(SimpleNamespace(restart=lambda _s: None), self.settings)
        self.addCleanup(self.screen.close)
        self.picker = self.screen.hrm_server

    def test_saving_keeps_the_mode_and_addresses(self):
        self.picker.buttons[servers.OFFICE].click()
        self.picker.online_edit.setText(f"{ONLINE}/api")
        self.picker.office_edit.setText(f"{OFFICE}/api")
        self.assertTrue(self.screen.apply())
        self.assertEqual(servers.profile(Settings.load(self.path)), servers.Profile(servers.OFFICE, ONLINE, OFFICE))
        self.assertEqual(self.picker.online_edit.text(), ONLINE)
        self.assertEqual(config_from_settings(self.settings).base_url, OFFICE)

    def test_a_bad_address_is_refused_and_nothing_is_saved(self):
        self.picker.office_edit.setText("ftp://nope")
        self.assertFalse(self.screen.apply())
        self.assertFalse(self.path.exists())

    def test_an_old_single_address_shows_in_its_slot(self):
        self.settings.hrm_api_url = OFFICE
        self.screen.load_settings(self.settings)
        self.assertEqual(self.picker.office_edit.text(), OFFICE)
        self.assertTrue(self.picker.buttons[servers.OFFICE].isChecked())

    def test_an_internet_http_address_warns(self):
        self.picker.online_edit.setText("http://hrm.example.com")
        self.assertIn("unencrypted", self.picker.warning.text())
        self.picker.online_edit.setText(ONLINE)
        self.assertEqual(self.picker.warning.text(), "")

    def test_the_key_test_sends_the_key_only_after_the_server_proves_itself(self):
        from face_attendance.dashboard.screens import settings as screen
        with patch.object(screen, "hrm_check_connection") as check:
            with self.assertRaises(screen.HRMError):
                screen._test_hrm_key(servers.Profile(servers.ONLINE, ONLINE, OFFICE), "k" * 40)
        check.assert_not_called()
        with patch.object(servers, "_open", network({ONLINE: HRM})), \
                patch.object(screen, "hrm_check_connection", return_value=["front"]) as check:
            self.assertEqual(screen._test_hrm_key(servers.Profile(servers.ONLINE, ONLINE, OFFICE), "k" * 40), ["front"])
        self.assertEqual(check.call_args.args[0].base_url, ONLINE)


class FollowsTheNetworkTests(unittest.TestCase):
    """The main window reconnects when Automatic finds the computer on another network."""

    def _window(self, url):
        from face_attendance.dashboard import app
        window = SimpleNamespace(
            settings=saved(hrm_server_mode=servers.AUTO, hrm_online_url=ONLINE, hrm_office_url=OFFICE,
                           hrm_api_key="k"),
            hrm_live=Mock(), _note=Mock(), _server_url=url)
        return window, app.MainWindow._server_checked

    def test_a_new_network_reconnects_the_live_link(self):
        window, checked = self._window(OFFICE)
        connection = servers.resolve(servers.profile(window.settings), network({ONLINE: HRM}))
        checked(window, 0, connection)
        self.assertEqual(window._server_url, ONLINE)
        self.assertEqual(window.hrm_live.configure.call_args.args[0].base_url, ONLINE)

    def test_no_answer_or_the_same_network_changes_nothing(self):
        window, checked = self._window(OFFICE)
        checked(window, 0, servers.resolve(servers.profile(window.settings), network({})))
        checked(window, 0, servers.resolve(servers.profile(window.settings), network({OFFICE: HRM})))
        window.hrm_live.configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
