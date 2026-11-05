"""Dashboard sign-in through SV HRM, HRM data in the Live Monitor, and HRM people in AI Face Angles."""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from face_attendance import hrm_auth, hrm_directory as hd
from face_attendance.catalog import FaceCatalog
from face_attendance.enrollment import write_employee, write_encodings
from face_attendance.hrm_sync import HRMConfig, HRMError
from face_attendance.models import Employee
from face_attendance.users import AccountDisabledError, Session


def jwt(sub="dara", roles=("ROLE_ADMIN",), expires_in=3600):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    claims = {"sub": sub, "roles": list(roles)}
    if expires_in is not None:
        claims["exp"] = time.time() + expires_in
    return f"{part({'alg': 'HS256'})}.{part(claims)}.signature"


class FakeHRM:
    """A real HTTP server speaking /api/auth/login and /api/users/check-username."""

    def __init__(self):
        fake = self
        self.logins, self.login_answer, self.check_answer, self.check_auth = [], (200, {}), (200, {"exists": True}), []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                fake.logins.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self._send(*fake.login_answer)

            def do_GET(self):
                fake.check_auth.append(self.headers.get("Authorization"))
                self._send(*fake.check_answer)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.config = HRMConfig(base_url=f"http://127.0.0.1:{self.server.server_port}")

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RoleMappingTests(unittest.TestCase):
    def test_admin_gets_everything_except_local_user_management(self):
        role, permissions = hrm_auth.access_for_roles(["ROLE_ADMIN"])
        self.assertEqual(role, "admin")
        self.assertIn("manage_settings", permissions)
        self.assertNotIn("manage_users", permissions)

    def test_an_ordinary_employee_gets_no_access(self):
        for roles in (["ROLE_EMPLOYEE"], ["ROLE_USER", "ROLE_KONKLA"], [], None):
            self.assertEqual(hrm_auth.access_for_roles(roles), ("", []))

    def test_only_the_exact_admin_authority_counts(self):
        self.assertEqual(hrm_auth.access_for_roles(["ADMIN", "role_admin", "ROLE_ADMINISTRATOR"]), ("", []))

    def test_manager_cannot_change_settings_or_enroll(self):
        role, permissions = hrm_auth.access_for_roles(["ROLE_MANAGER"])
        self.assertEqual((role, permissions), ("user", ["view_dashboard", "view_reports"]))


class SignInTests(unittest.TestCase):
    def setUp(self):
        self.hrm = FakeHRM()
        self.addCleanup(self.hrm.close)

    def test_success_builds_a_user_and_registers_the_device(self):
        token = jwt()
        self.hrm.login_answer = (200, {"accessToken": token, "userId": "7", "username": "dara",
                                       "employeeName": "Dara Kim", "roles": ["ROLE_ADMIN"]})
        user = hrm_auth.sign_in(self.hrm.config, "dara", "secret")
        self.assertEqual((user.uuid, user.full_name, user.role, user.password_hash), ("hrm:7", "Dara Kim", "admin", ""))
        self.assertTrue(user.external)
        # HRM rejects every later call with this token unless the login registered a device.
        self.assertTrue(self.hrm.logins[0]["deviceInfo"]["deviceId"])
        self.assertNotIn(token, repr(user))

    def test_wrong_username_and_wrong_password_look_the_same(self):
        for kind in ("username", "password"):
            self.hrm.login_answer = (401, {"success": False, "message": "x", "errorType": kind})
            self.assertIsNone(hrm_auth.sign_in(self.hrm.config, "dara", "bad"))

    def test_disabled_account_is_reported(self):
        self.hrm.login_answer = (401, {"message": "Authentication failed: User is disabled", "errorType": "other"})
        with self.assertRaises(AccountDisabledError):
            hrm_auth.sign_in(self.hrm.config, "dara", "secret")

    def test_account_without_a_role_cannot_sign_in(self):
        self.hrm.login_answer = (200, {"accessToken": jwt(), "username": "dara", "roles": ["ROLE_EMPLOYEE"]})
        with self.assertRaises(hrm_auth.NoAccessError):
            hrm_auth.sign_in(self.hrm.config, "dara", "secret")

    def test_unreachable_server_is_an_error_not_a_wrong_password(self):
        self.hrm.close()
        self.addCleanup(lambda: None)
        with self.assertRaises(HRMError):
            hrm_auth.sign_in(HRMConfig(base_url="http://127.0.0.1:9"), "dara", "secret")
        self.hrm = FakeHRM()

    def test_a_non_json_success_is_refused(self):
        self.hrm.login_answer = (200, {"unexpected": True})
        with self.assertRaises(HRMError):
            hrm_auth.sign_in(self.hrm.config, "dara", "secret")

    def test_passwords_are_never_sent_over_public_http(self):
        opener = Mock()
        with self.assertRaises(HRMError):
            hrm_auth.sign_in(HRMConfig(base_url="http://45.201.196.19:8082"), "dara", "secret", opener=opener)
        opener.assert_not_called()


class RememberedSessionTests(unittest.TestCase):
    def setUp(self):
        self.hrm = FakeHRM()
        self.addCleanup(self.hrm.close)

    @staticmethod
    def _session(token, username="dara"):
        return Session("hrm:7", username, "Dara Kim", "user", "0" * 64, "2026-01-01T00:00:00+00:00", token)

    def test_roles_come_from_the_token_once_hrm_accepts_it(self):
        token = jwt(roles=["ROLE_MANAGER"])
        user = hrm_auth.restore(self.hrm.config, self._session(token))
        self.assertEqual(user.permissions, ["view_dashboard", "view_reports"])
        self.assertEqual(self.hrm.check_auth, [f"Bearer {token}"])

    def test_a_token_hrm_rejects_is_not_trusted(self):
        self.hrm.check_answer = (401, {})
        self.assertIsNone(hrm_auth.restore(self.hrm.config, self._session(jwt())))

    def test_a_disabled_account_is_not_restored(self):
        self.hrm.check_answer = (200, {"exists": False})
        self.assertIsNone(hrm_auth.restore(self.hrm.config, self._session(jwt())))

    def test_expired_mismatched_or_old_local_sessions_need_sign_in(self):
        for session in (self._session(jwt(expires_in=-5)), self._session(jwt(sub="other")), self._session("")):
            self.assertIsNone(hrm_auth.restore(self.hrm.config, session))
        self.assertEqual(self.hrm.check_auth, [], "these never reach HRM")

    def test_offline_hrm_does_not_grant_access(self):
        with self.assertRaises(HRMError):
            hrm_auth.restore(HRMConfig(base_url="http://127.0.0.1:9"), self._session(jwt()))

    def test_session_file_keeps_the_token_private(self):
        from face_attendance import users
        from face_attendance.users import User, load_session, save_session
        with tempfile.TemporaryDirectory() as directory, patch.object(users, "SESSION_PATH", Path(directory) / ".session"):
            token = jwt()
            save_session(User("hrm:7", "dara", "Dara", "", "", "", "admin", hrm_token=token))
            self.assertEqual(load_session().hrm_token, token)
            self.assertEqual(oct(users.SESSION_PATH.stat().st_mode)[-3:], "600")


class TokensWithoutExpiryTests(unittest.TestCase):
    """What SV HRM really issues: no ``exp``, valid until HRM ends the device's session."""

    def setUp(self):
        self.hrm = FakeHRM()
        self.addCleanup(self.hrm.close)
        self.token = jwt(expires_in=None)

    def _session(self):
        return Session("hrm:7", "dara", "Dara Kim", "user", "0" * 64, "2026-01-01T00:00:00+00:00", self.token)

    def test_only_unreadable_or_past_tokens_count_as_expired(self):
        self.assertFalse(hrm_auth.token_expired(self.token))
        self.assertFalse(hrm_auth.token_expired(jwt(expires_in=60)))
        self.assertTrue(hrm_auth.token_expired(jwt(expires_in=-5)))
        for broken in ("", "not-a-token", "a.b.c"):
            self.assertTrue(hrm_auth.token_expired(broken))

    def test_a_remembered_session_is_restored_once_hrm_accepts_it(self):
        user = hrm_auth.restore(self.hrm.config, self._session())
        self.assertEqual(user.username, "dara")
        self.assertEqual(self.hrm.check_auth, [f"Bearer {self.token}"])

    def test_it_is_still_hrm_that_decides(self):
        self.hrm.check_answer = (401, {})
        self.assertIsNone(hrm_auth.restore(self.hrm.config, self._session()))

    def test_the_running_dashboard_hears_about_a_disabled_or_ended_account(self):
        from face_attendance.users import User
        user = User("hrm:7", "dara", "Dara", "", "", "", "admin", hrm_token=self.token)
        self.assertIs(hrm_auth.check_account(self.hrm.config, user), user)
        self.hrm.check_answer = (200, {"exists": False})
        self.assertTrue(hrm_auth.check_account(self.hrm.config, user).disabled)
        self.hrm.check_answer = (401, {})
        self.assertIsNone(hrm_auth.check_account(self.hrm.config, user))


class WatcherTests(unittest.TestCase):
    def test_hrm_outage_or_expired_token_keeps_a_running_dashboard_signed_in(self):
        from face_attendance.users import User
        user = User("hrm:7", "dara", "Dara", "", "", "", "admin", hrm_token=jwt(expires_in=-5))
        with self.assertRaises(HRMError):
            hrm_auth.check_account(HRMConfig(base_url="http://127.0.0.1:9"), user)
        hrm = FakeHRM()
        self.addCleanup(hrm.close)
        with self.assertRaises(HRMError):
            hrm_auth.check_account(hrm.config, user)
        self.assertEqual(hrm.check_auth, [])

    def test_disabled_in_hrm_marks_the_account_disabled(self):
        from face_attendance.users import User
        hrm = FakeHRM()
        self.addCleanup(hrm.close)
        hrm.check_answer = (200, {"exists": False})
        user = User("hrm:7", "dara", "Dara", "", "", "", "admin", hrm_token=jwt())
        self.assertTrue(hrm_auth.check_account(hrm.config, user).disabled)


def directory(*rows, fetched_at=100.0):
    return hd.Directory(tuple(rows), fetched_at, "http://hrm")


def hrm_row(code, name="Sok Dara", status="ACTIVE", **extra):
    return {"employee_id": code, "employee_name": name, "first_name": name.split()[0], "last_name": name.split()[-1],
            "hrm_status": status, "enrolled": False, **extra}


class LiveMonitorDirectoryTests(unittest.TestCase):
    def test_name_and_status_come_from_hrm(self):
        resolve = hd.resolver(directory(hrm_row("E1", "Sok Dara")))
        employee = resolve(Employee("e1", "old name"))
        self.assertEqual((employee.name, employee.first_name, employee.last_name, employee.active),
                         ("Sok Dara", "Sok", "Dara", True))

    def test_inactive_in_hrm_or_missing_from_hrm_is_blocked(self):
        resolve = hd.resolver(directory(hrm_row("E1", status="TERMINATED")))
        self.assertFalse(resolve(Employee("E1", "x")).active)
        self.assertFalse(resolve(Employee("E2", "x")).active)

    def test_marking_someone_inactive_here_still_holds(self):
        resolve = hd.resolver(directory(hrm_row("E1")))
        self.assertFalse(resolve(Employee("E1", "x", active=False)).active)

    def test_no_directory_keeps_the_local_list(self):
        self.assertIsNone(hd.resolver(hd.Directory()))
        self.assertIsNone(hd.resolver(directory()))

    def test_catalog_uses_the_resolved_details_for_recognition(self):
        with tempfile.TemporaryDirectory() as folder:
            encodings, employees = Path(folder) / "faces.pickle", Path(folder) / "employees.json"
            write_encodings(encodings, {"local": [np.ones(128)], "gone": [np.ones(128) * 2]})
            write_employee(employees, "local", "E1", "Local Name")
            write_employee(employees, "gone", "E2", "Gone Person")
            catalog = FaceCatalog(encodings, employees, resolve=hd.resolver(directory(hrm_row("E1", "Sok Dara"))))
            found = {employee.employee_id: employee for employee in catalog.employees}
            self.assertEqual(found["E1"].name, "Sok Dara")
            self.assertTrue(found["E1"].active)
            self.assertFalse(found["E2"].active)
            self.assertEqual(catalog.employee_map["local"].name, "Sok Dara")

    def test_fingerprint_ignores_when_it_synced_but_sees_a_status_change(self):
        base = directory(hrm_row("E1"))
        self.assertEqual(hd.fingerprint(base), hd.fingerprint(replace(base, fetched_at=999.0)))
        self.assertNotEqual(hd.fingerprint(base), hd.fingerprint(directory(hrm_row("E1", status="INACTIVE"))))


class AnglesDirectoryTests(unittest.TestCase):
    def setUp(self):
        from PyQt6.QtWidgets import QApplication
        from face_attendance.settings import Settings
        self.app = QApplication.instance() or QApplication([])
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        root = Path(self._folder.name)
        self.settings = Settings(encodings_path=str(root / "faces.pickle"), employees_path=str(root / "employees.json"),
                                 db_path=str(root / "a.db"), theme="light", hrm_api_url="http://127.0.0.1:9",
                                 hrm_api_key="k" * 32)
        hd._atomic_json(hd.cache_path(self.settings), {
            "version": hd.CACHE_VERSION, "fetched_at": 100.0, "source": "x",
            "employees": [hrm_row("E1", "Sok Dara", has_photo=False), hrm_row("E2", "Chan Mey", status="INACTIVE")]})

    def _screen(self):
        from face_attendance.dashboard.screens.ai_angles import AIAnglesScreen
        screen = AIAnglesScreen(Mock(), self.settings)
        self.addCleanup(screen.close)
        return screen

    def test_people_who_only_exist_in_hrm_can_be_chosen_and_generated(self):
        screen = self._screen()
        self.assertEqual(screen.employee_combo.count(), 2)
        self.assertIn("Chan Mey (E2) (inactive)", screen.employee_combo.itemText(0))
        screen.employee_combo.setCurrentIndex(1)
        self.assertEqual(screen._person_label(), "Sok Dara")
        self.assertEqual(screen._employee_code(), "E1")

    def test_selection_survives_a_reload(self):
        screen = self._screen()
        screen.employee_combo.setCurrentIndex(1)
        screen._reload_employees()
        self.assertEqual(screen._employee_code(), "E1")

    def test_hrm_photo_is_downloaded_for_the_chosen_person(self):
        import cv2
        ok, jpeg = cv2.imencode(".jpg", np.full((80, 80, 3), 120, np.uint8))
        self.assertTrue(ok)
        screen = self._screen()
        with patch("face_attendance.hrm_directory.hrm_sync.employee_photo", return_value=jpeg.tobytes()):
            row = dict(screen.employee_combo.itemData(1), has_photo=True)
            screen.employee_combo.setItemData(1, row)
            screen.employee_combo.setCurrentIndex(1)
            deadline = time.monotonic() + 5
            while screen.source is None and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.01)
        self.assertIsNotNone(screen.source)

    def test_a_photo_for_someone_no_longer_chosen_is_ignored(self):
        screen = self._screen()
        screen.employee_combo.setCurrentIndex(1)
        screen._on_hrm_photo("E2", Path("/nonexistent"))
        self.assertIsNone(screen.source)

    def test_first_enrollment_of_an_hrm_person_starts_from_their_real_photo(self):
        from PyQt6.QtWidgets import QMessageBox
        screen = self._screen()
        screen.employee_combo.setCurrentIndex(1)
        screen.source = np.full((80, 80, 3), 120, np.uint8)
        screen.runner = Mock()
        with patch.object(screen, "_included_paths", return_value=["a.jpg"]), \
                patch("face_attendance.dashboard.screens.ai_angles.read_image", return_value=np.zeros((80, 80, 3), np.uint8)), \
                patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes):
            screen._add_to_enrollment()
        images, name, code, _check, first, last = screen.runner.submit.call_args.args
        self.assertIs(images[0], screen.source)
        self.assertEqual((name, code, first, last), ("Sok Dara", "E1", "Sok", "Dara"))


class SignedInWindowTests(unittest.TestCase):
    def test_hrm_account_is_not_tied_to_the_local_users_table(self):
        from tests.test_dashboard import _app, temp_settings
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.users import User
        _app()
        with tempfile.TemporaryDirectory() as folder:
            role, permissions = hrm_auth.access_for_roles(["ROLE_ADMIN"])
            user = User("hrm:7", "dara", "Dara", "", "", "", role, permissions, hrm_token=jwt())
            window = MainWindow(temp_settings(folder), use_lock=False, current_user=user)
            try:
                self.assertTrue(window.access.allows_nav("Live Monitor"))
            finally:
                window.close()
                _app().processEvents()


if __name__ == "__main__":
    unittest.main()
