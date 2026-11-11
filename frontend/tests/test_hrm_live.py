"""The HRM live link against a real local WebSocket server, and the Live Monitor's in-place reload."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtNetwork import QHostAddress
from PyQt6.QtWebSockets import QWebSocketServer
from PyQt6.QtWidgets import QApplication

from face_attendance import angle_templates as at
from face_attendance import hrm_directory as hd
from face_attendance.dashboard import hrm_live
from face_attendance.hrm_sync import HRMConfig
from tests.test_dashboard import FakeBackend, temp_settings, _app


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return False


class ParsingTests(unittest.TestCase):
    def test_events_url_follows_the_hrm_address(self):
        self.assertEqual(hrm_live.events_url("http://10.0.0.5:8082/"),
                         "ws://10.0.0.5:8082/api/face-recognition/events")
        self.assertEqual(hrm_live.events_url("https://hrm.example.com"),
                         "wss://hrm.example.com/api/face-recognition/events")
        self.assertEqual(hrm_live.events_url("ftp://x"), "")
        self.assertEqual(hrm_live.events_url(""), "")

    def test_only_known_well_formed_events_count(self):
        self.assertEqual(hrm_live.parse_event('{"type":"employee.changed","employeeCode":" E1 "}'),
                         ("employee.changed", "E1"))
        self.assertEqual(hrm_live.parse_event('{"type":"angles.changed","employeeCode":"E1"}'),
                         ("angles.changed", "E1"))
        for bad in ("nope", "[]", '{"type":"other","employeeCode":"E1"}', '{"type":"employee.changed"}',
                    '{"type":"employee.changed","employeeCode":5}', " " * 5000):
            self.assertIsNone(hrm_live.parse_event(bad), bad)


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.application = _app()  # Keep the Qt event dispatcher alive in isolated runs.
        self.server = QWebSocketServer("test", QWebSocketServer.SslMode.NonSecureMode)
        self.assertTrue(self.server.listen(QHostAddress.SpecialAddress.LocalHost))
        self.clients = []
        self.keys = []
        self.server.newConnection.connect(self._accept)
        self.config = HRMConfig(f"http://127.0.0.1:{self.server.serverPort()}", "k" * 40)
        self.link = hrm_live.HrmLiveLink(settle_ms=20, reconnect_ms=20)
        self.refreshes, self.angles, self.states = [], [], []
        self.link.employeesChanged.connect(lambda: self.refreshes.append(1))
        self.link.anglesChanged.connect(self.angles.append)
        self.link.connectedChanged.connect(self.states.append)

    def tearDown(self):
        self.link.stop()
        self.server.close()

    def _accept(self):
        socket = self.server.nextPendingConnection()
        self.keys.append(bytes(socket.request().rawHeader(b"X-API-KEY")).decode())
        self.clients.append(socket)

    def _connect(self):
        self.link.configure(self.config)
        self.assertTrue(wait_for(lambda: self.link.connected and self.clients))

    def test_sends_the_key_and_refreshes_once_connected(self):
        self._connect()
        self.assertEqual(self.keys, ["k" * 40])
        self.assertEqual(self.refreshes, [1])

    def test_several_changes_become_one_refresh(self):
        self._connect()
        self.refreshes.clear()
        for code in ("E1", "E2", "E3"):
            self.clients[0].sendTextMessage(json.dumps({"type": "employee.changed", "employeeCode": code}))
        self.assertTrue(wait_for(lambda: self.refreshes))
        wait_for(lambda: False, 0.1)
        self.assertEqual(self.refreshes, [1])

    def test_photo_changes_name_the_employee_and_do_not_count_as_angles(self):
        photos = []
        self.link.photoChanged.connect(photos.append)
        self._connect()
        self.clients[0].sendTextMessage(json.dumps({"type": "photo.changed", "employeeCode": "E7"}))
        self.assertTrue(wait_for(lambda: photos))
        self.assertEqual(photos, ["E7"])
        self.assertEqual(self.angles, [])

    def test_new_face_checks_name_the_employee_and_do_not_count_as_changed_angles(self):
        verified = []
        self.link.anglesVerified.connect(verified.append)
        self._connect()
        self.clients[0].sendTextMessage(json.dumps({"type": "angles.verified", "employeeCode": "E7"}))
        self.assertTrue(wait_for(lambda: verified))
        self.assertEqual(verified, ["E7"])
        self.assertEqual(self.angles, [])

    def test_angle_changes_name_the_employee(self):
        self._connect()
        self.clients[0].sendTextMessage(json.dumps({"type": "angles.changed", "employeeCode": "E7"}))
        self.assertTrue(wait_for(lambda: self.angles))
        self.assertEqual(self.angles, ["E7"])

    def test_reconnects_and_refreshes_after_a_drop(self):
        self._connect()
        self.clients[0].close()
        self.assertTrue(wait_for(lambda: len(self.clients) == 2 and self.link.connected))
        self.assertEqual(self.states, [True, False, True])
        self.assertEqual(self.refreshes, [1, 1])

    def test_unconfigured_never_connects_and_stop_stays_stopped(self):
        self.link.configure(HRMConfig(self.config.base_url, ""))
        wait_for(lambda: False, 0.1)
        self.assertEqual(self.clients, [])
        self._connect()
        self.link.stop()
        wait_for(lambda: False, 0.1)
        self.assertEqual(len(self.clients), 1)
        self.assertFalse(self.link.connected)


class InPlaceReloadTests(unittest.TestCase):
    """A change reaches recognition with the camera and workers left running."""

    def setUp(self):
        from face_attendance.dashboard.engine import AttendanceEngine
        from face_attendance.enrollment import write_employee
        _app()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        settings = temp_settings(self.root)
        write_employee(self.root / "employees.json", "Seed", "E1")
        self.engine = AttendanceEngine(settings, backend=FakeBackend(), use_lock=False)
        self.engine._thread = _AliveThread()     # Treated as running; nothing real is started.
        self.addCleanup(lambda: setattr(self.engine, "_thread", None))

    def _write_directory(self, name, status="ACTIVE"):
        hd._atomic_json(hd.cache_path(self.engine.config), {
            "version": hd.CACHE_VERSION, "fetched_at": 1.0, "source": "x",
            "employees": [{"employee_id": "E1", "employee_name": name, "first_name": "", "last_name": "",
                           "hrm_status": status}]})
        self._write_angles({"left": np.ones(128)})

    def _write_angles(self, angles, code="E1"):
        at._save(at.store_path(self.engine.config), {code: {
            angle: {"sha256": angle, "version": 1, "problem": "", "check": "matched", "vector": at._vector_text(vector)}
            for angle, vector in angles.items()}})

    def _matched(self):
        return self.engine.recognition.service.match(np.ones(128))[0]

    def test_a_new_hrm_name_reaches_recognition_without_replacing_workers(self):
        camera, recognition, persistence = self.engine.camera, self.engine.recognition, self.engine.persistence
        self._write_directory("New Name")
        self.assertTrue(self.engine.reload_if_directory_changed())
        self.assertEqual(self._matched().name, "New Name")
        self.assertIs(self.engine.camera, camera)
        self.assertIs(self.engine.recognition, recognition)
        self.assertIs(self.engine.persistence, persistence)
        self.assertFalse(self.engine.reload_if_directory_changed())

    def test_with_hrm_connected_only_enrolled_angles_are_matched(self):
        self._write_directory("Seed Person")
        self.engine.reload_if_directory_changed()
        self.assertEqual(self.engine.catalog.enrolled_count, 1)
        self.assertEqual(len(self.engine.catalog.encodings), 1)     # Not the Seed sample from faces.pickle.
        self._write_angles({"left": np.ones(128), "up": np.ones(128)})
        self.engine.reload_catalog()
        self.assertEqual(len(self.engine.catalog.encodings), 2)
        self._write_angles({})
        self.engine.reload_catalog()
        self.assertIsNone(self._matched())

    def test_changed_angles_are_noticed_by_the_catalog_poll(self):
        self._write_directory("Seed Person")
        self.engine.reload_if_directory_changed()
        self.engine.reload_if_catalog_changed()
        self.engine.reload_if_catalog_changed()
        before = self.engine.catalog
        self._write_angles({"left": np.ones(128), "up": np.ones(128)})
        self.engine.reload_if_catalog_changed()     # Seen once: could be a save still going on.
        self.engine.reload_if_catalog_changed()
        self.assertIsNot(self.engine.catalog, before)
        self.assertEqual(len(self.engine.catalog.encodings), 2)

    def test_a_status_change_blocks_attendance_right_away(self):
        self._write_directory("Seed Person", "TERMINATED")
        self.engine.reload_if_directory_changed()
        self.assertFalse(self._matched().active)

    def test_new_face_samples_are_used_and_the_saved_map_stays_shared(self):
        from face_attendance.enrollment import write_encodings, write_employee
        shared = self.engine.persistence.employee_map
        write_encodings(self.root / "faces.pickle", {"Seed": [np.ones(128)], "Second": [np.zeros(128)]})
        write_employee(self.root / "employees.json", "Second", "E2")
        self.engine.reload_catalog()
        self.assertEqual(self.engine.catalog.enrolled_count, 2)
        self.assertIs(self.engine.persistence.employee_map, shared)
        self.assertIn("Second", shared)

    def test_an_unreadable_catalog_keeps_the_current_one_and_reports_once(self):
        errors = []
        self.engine.errorRaised.connect(errors.append)
        before = self.engine.catalog
        (self.root / "faces.pickle").write_bytes(b"not a catalog")
        self.engine.reload_catalog()
        self.assertIs(self.engine.catalog, before)
        self.assertEqual(len(errors), 1)
        self.assertFalse(self.engine.reload_if_catalog_changed())


class _AliveThread:
    def is_alive(self):
        return True

    def join(self, *_):
        pass


class MergeNameTests(unittest.TestCase):
    def test_the_table_shows_the_hrm_name_the_live_monitor_uses(self):
        directory = hd.Directory((hd.normalize({"id": 1, "employeeCode": "E1", "firstName": "Sok", "lastName": "Dara"}),), 1.0)
        local = [{"name": "Old Label", "employee_id": "e1", "employee_name": "Old Name", "samples": 2, "active": True},
                 {"name": "Guest", "employee_id": "L1", "employee_name": "Guest", "samples": 1, "active": True}]
        rows = {row["employee_id"]: row for row in hd.merge(local, directory)}
        self.assertEqual(rows["e1"]["employee_name"], "Sok Dara")
        self.assertEqual(rows["e1"]["name"], "Old Label")
        self.assertEqual(rows["L1"]["employee_name"], "Guest")


if __name__ == "__main__":
    unittest.main()
