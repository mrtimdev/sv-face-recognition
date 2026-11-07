"""What attendance runs on once HRM owns employees: one catalog everywhere, and enrolling that counts."""
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from face_attendance import angle_templates, hrm_directory as hd, hrm_sync
from face_attendance.settings import Settings
from tests.test_dashboard import _app, temp_settings


def write_directory(settings, *rows):
    employees = [hd.normalize({"id": index, "employeeCode": code, "firstName": name, "status": status})
                 for index, (code, name, status) in enumerate(rows)]
    Path(hd.cache_path(settings)).write_text(json.dumps(
        {"version": hd.CACHE_VERSION, "fetched_at": time.time(), "source": "x", "employees": employees}),
        encoding="utf-8")


def write_angles(settings, *codes):
    vector = angle_templates._vector_text(np.ones(128))
    angle_templates._save(angle_templates.store_path(settings),
                          {code: {"straight": {"sha256": "x", "vector": vector, "check": "matched"}} for code in codes})


class LiveCatalogTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = Settings(employees_path=str(Path(folder.name) / "employees.json"),
                                 encodings_path=str(Path(folder.name) / "encodings_sface.pickle"))

    def test_connected_catalog_uses_hrm_angles_and_leaves_out_who_hrm_does_not_list(self):
        write_directory(self.settings, ("EMP1", "Dara", "ACTIVE"))
        write_angles(self.settings, "emp1", "GONE9")
        catalog = hd.live_catalog(self.settings)
        people = {employee.employee_id.casefold(): employee for employee in catalog.employees}
        self.assertEqual(people["emp1"].name, "Dara")
        self.assertTrue(people["emp1"].active)
        self.assertNotIn("gone9", people)
        self.assertEqual(catalog.enrolled_count, 1)

    def test_a_new_employee_list_changes_who_the_catalog_has(self):
        write_directory(self.settings, ("EMP1", "Dara", "ACTIVE"), ("EMP2", "Sok", "ACTIVE"))
        write_angles(self.settings, "EMP1", "EMP2")
        self.assertEqual(hd.live_catalog(self.settings).enrolled_count, 2)
        write_directory(self.settings, ("EMP2", "Sok", "ACTIVE"))
        self.assertEqual(hd.live_catalog(self.settings).enrolled_count, 1)

    def test_a_finished_sync_or_override_updates_the_angles_and_then_the_live_monitor(self):
        from face_attendance.dashboard.app import MainWindow
        window = SimpleNamespace(hrm_angles=MagicMock(), engine=MagicMock(), screens={2: MagicMock(), 6: MagicMock()},
                                 hrm_live=MagicMock(connected=True), _exit=None, _exit_done=False)
        MainWindow._on_hrm_directory_synced(window, RuntimeError("HRM is down"))
        window.hrm_angles.request.assert_not_called()
        MainWindow._on_hrm_directory_synced(window, hd.Directory())
        window.hrm_angles.request.assert_called_once()

        MainWindow._on_angle_templates_updated(window, SimpleNamespace(changed=False, changed_codes=()))
        window.engine.reload_catalog.assert_not_called()
        MainWindow._on_angle_templates_updated(window, SimpleNamespace(changed=True, changed_codes=("E1",)))
        window.engine.reload_catalog.assert_called_once()
        window.screens[6].on_hrm_angles_changed.assert_not_called()   # A live event already said so.

        window.hrm_angles.reset_mock()
        MainWindow._on_server_changed(window)
        window.screens[2].request_hrm_refresh.assert_called_once()
        window.hrm_angles.request.assert_called_once()

    def test_without_the_live_connection_the_app_keeps_checking_hrm(self):
        from face_attendance.dashboard.app import MainWindow
        window = SimpleNamespace(hrm_angles=MagicMock(), engine=MagicMock(), screens={2: MagicMock(), 6: MagicMock()},
                                 hrm_live=MagicMock(connected=True), _exit=None, _exit_done=False)
        MainWindow._refresh_without_live(window)
        window.screens[2].request_hrm_refresh.assert_not_called()
        window.hrm_live.connected = False
        MainWindow._refresh_without_live(window)
        window.screens[2].request_hrm_refresh.assert_called_once()

        MainWindow._on_angle_templates_updated(window, SimpleNamespace(changed=True, changed_codes=("E1", "E2")))
        self.assertEqual([call.args[0] for call in window.screens[6].on_hrm_angles_changed.call_args_list], ["E1", "E2"])
        self.assertEqual([call.args[0] for call in window.screens[2].on_hrm_angles_changed.call_args_list], ["E1", "E2"])

    def test_terminal_uses_the_same_catalog_as_the_dashboard(self):
        from face_attendance.runtime import AttendanceApplication

        class Stop(Exception):
            pass
        with patch("face_attendance.runtime.hrm_directory.live_catalog", side_effect=Stop) as catalog:
            with self.assertRaises(Stop):
                AttendanceApplication(self.settings.to_config())
        catalog.assert_called_once()

    def test_hrm_photo_cache_is_private(self):
        data = b"\xff\xd8" + b"0" * 32
        with patch.object(hrm_sync, "employee_photo", return_value=data):
            path = hd.fetch_photo(hrm_sync.HRMConfig("http://127.0.0.1:9", "k"), self.settings, "EMP1")
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)


class EnrollButtonTests(unittest.TestCase):
    def setUp(self):
        from face_attendance.dashboard.app import MainWindow
        _app()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = temp_settings(folder.name)
        write_directory(self.settings, ("EMP1", "Dara", "ACTIVE"))
        self.window = MainWindow(self.settings, use_lock=False)
        self.addCleanup(self.window.close)
        self.employees = self.window.screens[2]
        self.row = {"employee_id": "EMP1", "employee_name": "Dara", "enrolled": False}

    def test_goes_to_face_angles_when_matching_on_angles(self):
        with patch.object(self.employees, "selected_row", return_value=self.row), \
                patch.object(self.employees, "_angle_counts", return_value={}), \
                patch.object(self.employees, "_open_enrollment") as local:
            self.employees._enroll_selected_from_hrm()
        local.assert_not_called()
        angles = self.window.screens[6]
        self.assertIs(self.window.stack.currentWidget(), angles)
        self.assertEqual(angles._current_employee()["employee_id"], "EMP1")

    def test_stays_on_local_samples_before_hrm_is_connected(self):
        with patch.object(self.employees, "selected_row", return_value=self.row), \
                patch.object(self.employees, "_angle_counts", return_value=None), \
                patch.object(self.employees, "_open_enrollment") as local:
            try:
                self.employees._enroll_selected_from_hrm()
            except AttributeError:
                pass  # The wizard's fields aren't prepared with enrollment patched out.
        local.assert_called_once()
        self.assertIsNot(self.window.stack.currentWidget(), self.window.screens[6])


if __name__ == "__main__":
    unittest.main()
