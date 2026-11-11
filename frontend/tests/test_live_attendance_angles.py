"""The Live Monitor matches HRM's angles, and check-ins reach HRM over the socket."""
import hashlib
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from face_attendance import angle_templates as at
from face_attendance import hrm_directory as hd
from face_attendance import hrm_sync
from face_attendance.catalog import FaceCatalog
from face_attendance.dashboard import hrm_attendance, hrm_live
from face_attendance.hrm_sync import HRMConfig, HRMError
from face_attendance.models import CaptureJob, Employee
from face_attendance.repository import AttendanceRepository
from face_attendance.storage import SnapshotService
from tests.test_dashboard import EmployeesScreenTests, FakeBackend, _app

CONFIG = HRMConfig("http://127.0.0.1:9", "k" * 40)


def jpeg(value=120, size=(80, 80)):
    ok, data = cv2.imencode(".jpg", np.full((*size, 3), value, dtype=np.uint8))
    assert ok
    return data.tobytes()


def row(code, angle, content):
    return {"employeeCode": code, "angle": angle, "sha256": hashlib.sha256(content).hexdigest(), "version": 1,
            "verificationStatus": "matched"}


class OneFace:
    """Finds *faces* faces, and turns an image's brightness into a repeatable template."""

    def __init__(self, faces=1):
        self.faces, self.encoded = faces, 0

    def face_locations(self, image, model=None):
        return [(10, 60, 60, 10)] * self.faces

    def face_encodings(self, image, boxes):
        self.encoded += 1
        vector = np.full(128, float(image.mean()) + 1.0)
        return [vector / np.linalg.norm(vector)]


class Base(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.path = self.root / at.STORE_NAME

    def sync(self, rows, images, backend=None):
        with patch.object(hrm_sync, "angle_summary", return_value=rows), \
                patch.object(hrm_sync, "angle_image", side_effect=images) as download:
            result = at.sync(CONFIG, self.path, backend=backend or OneFace())
        return result, download


class StoreTests(Base):
    def test_builds_a_private_template_for_each_angle(self):
        image = jpeg()
        result, _ = self.sync([row("E1", "left", image), row("E1", "up", image)], [image, image])
        self.assertEqual((result.employees, result.changed, result.problems), (1, True, ()))
        self.assertEqual(at.counts(at.load(self.path)), {"e1": 2})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode) & 0o077, 0)

    def test_an_unchanged_image_is_neither_downloaded_nor_rewritten(self):
        image = jpeg()
        self.sync([row("E1", "left", image)], [image])
        before = self.path.stat().st_mtime_ns
        result, download = self.sync([row("E1", "left", image)], [])
        download.assert_not_called()
        self.assertFalse(result.changed)
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_a_replaced_image_gets_a_new_template_and_a_removed_one_goes(self):
        first, second = jpeg(60), jpeg(200)
        self.sync([row("E1", "left", first), row("E1", "up", first)], [first, first])
        old = at.load(self.path)["E1"]["left"]["vector"]
        result, _ = self.sync([row("E1", "left", second)], [second])
        self.assertTrue(result.changed)
        store = at.load(self.path)
        self.assertNotEqual(store["E1"]["left"]["vector"], old)
        self.assertNotIn("up", store["E1"])

    def test_an_image_without_exactly_one_face_cant_be_matched_and_says_why(self):
        image = jpeg()
        for faces, why in ((0, "No face"), (2, "More than one face")):
            result, _ = self.sync([row("E1", "left", image)], [image], OneFace(faces))
            self.assertIn(why, result.problems[0])
            store = at.load(self.path)
            self.assertEqual(at.counts(store), {})
            self.assertIn(why, at.problems_for(store, "e1")["left"])
            self.path.unlink()

    def test_a_failed_download_keeps_the_previous_template_and_tries_again(self):
        first, second = jpeg(60), jpeg(200)
        self.sync([row("E1", "left", first)], [first])
        result, _ = self.sync([row("E1", "left", second)], [HRMError("HTTP 500")])
        self.assertEqual(at.counts(at.load(self.path)), {"e1": 1})
        self.assertEqual(len(result.problems), 1)
        _, download = self.sync([row("E1", "left", second)], [second])
        download.assert_called_once()

    def test_a_download_that_fails_its_checksum_is_not_used(self):
        result, _ = self.sync([row("E1", "left", jpeg(60))], [jpeg(200)])
        self.assertEqual(at.counts(at.load(self.path)), {})
        self.assertIn("checksum", result.problems[0])

    def test_a_damaged_or_other_model_store_reads_as_empty(self):
        self.path.write_text("{ not json")
        self.assertEqual(at.load(self.path), {})
        self.path.write_text(json.dumps({"version": at.STORE_VERSION, "model": "other", "employees": {"E1": {}}}))
        self.assertEqual(at.load(self.path), {})


class CatalogTests(Base):
    def setUp(self):
        super().setUp()
        image = jpeg()
        self.sync([row("E1", "left", image), row("E1", "up", image), row("H2", "left", image)], [image] * 3)
        self.employees = self.root / "employees.json"
        self.employees.write_text(json.dumps({"Local": {"employee_id": "e1", "name": "Local", "status": "active"},
                                              "Other": {"employee_id": "E9", "name": "Other", "status": "active"}}))

    def _catalog(self, resolve=None):
        # A real samples file would be ignored; this one doesn't even exist.
        return FaceCatalog(self.root / "faces.pickle", self.employees, allow_empty=True, resolve=resolve,
                           angle_store=at.load(self.path))

    def test_only_angles_are_matched_and_someone_with_samples_only_isnt(self):
        catalog = self._catalog()
        self.assertEqual(len(catalog.encodings), 3)
        self.assertEqual(catalog.enrolled_count, 2)
        self.assertEqual({e.employee_id for e in catalog.employees}, {"e1", "H2"})
        self.assertIn("Other", catalog.employee_map)     # Kept for names on old records.

    def test_samples_on_disk_are_ignored(self):
        samples = self.root / "faces.pickle"
        from face_attendance.enrollment import write_encodings
        write_encodings(samples, {"Other": [np.full(128, 0.5)]})
        catalog = FaceCatalog(samples, self.employees, allow_empty=True, angle_store=at.load(self.path))
        self.assertNotIn("E9", {e.employee_id for e in catalog.employees})
        no_angles = FaceCatalog(samples, self.employees, allow_empty=True, angle_store={})
        self.assertEqual(len(no_angles.encodings), 0)
        legacy = FaceCatalog(samples, self.employees, allow_empty=True)
        self.assertEqual(len(legacy.encodings), 1)      # Never connected to HRM: local samples.

    def test_hrm_decides_name_and_status(self):
        directory = hd.Directory((hd.normalize({"id": 1, "employeeCode": "H2", "firstName": "Sok", "lastName": "Dara",
                                                "status": "ACTIVE"}),), 1.0)
        catalog = self._catalog(hd.resolver(directory))
        by_id = {e.employee_id: e for e in catalog.employees}
        self.assertEqual(by_id["H2"].name, "Sok Dara")
        self.assertTrue(by_id["H2"].active)
        self.assertFalse(by_id["e1"].active)            # HRM doesn't list them as active.

    def test_an_employee_marked_inactive_here_stays_inactive(self):
        self.employees.write_text(json.dumps({"Local": {"employee_id": "H2", "name": "L", "status": "inactive"}}))
        directory = hd.Directory((hd.normalize({"id": 1, "employeeCode": "H2", "firstName": "S",
                                                "status": "ACTIVE"}),), 1.0)
        catalog = self._catalog(hd.resolver(directory))
        self.assertFalse({e.employee_id: e for e in catalog.employees}["H2"].active)


class ScreenTests(Base):
    def setUp(self):
        super().setUp()
        self.application = _app()
        self.screen = EmployeesScreenTests()._screen(self._dir.name)
        self.addCleanup(self.screen.deleteLater)
        self.path = at.store_path(self.screen.settings)
        image = jpeg()
        self.sync([row("H1", "left", image), row("H1", "up", image)], [image, image])
        self.screen._hrm_directory = hd.Directory(
            (hd.normalize({"id": 1, "employeeCode": "H1", "firstName": "Has", "status": "ACTIVE"}),
             hd.normalize({"id": 2, "employeeCode": "H2", "firstName": "Not", "status": "ACTIVE"})), 1.0)
        self.screen.reload()

    def _row(self, code):
        return next(r for r in self.screen.directory_rows if r["employee_id"] == code)

    def test_the_table_counts_angles_and_ready_means_having_angles(self):
        self.assertEqual(self.screen.employee_table.horizontalHeaderItem(4).text(), "ANGLES")
        self.assertEqual(self._row("H1")["angles"], 2)
        self.assertEqual(self._row("H2")["angles"], 0)
        self.assertEqual([r["employee_id"] for r in hd.Filters(enrollment=hd.READY).apply(self.screen.directory_rows)],
                         ["H1"])
        self.assertEqual(self.screen.stat_samples.title_label.text(), "Face angles")

    def test_with_no_hrm_list_the_table_still_counts_samples(self):
        self.screen._hrm_directory = hd.Directory()
        self.screen.reload()
        self.assertEqual(self.screen.employee_table.horizontalHeaderItem(4).text(), "SAMPLES")

    def test_an_angle_that_cant_be_matched_is_named_in_the_profile(self):
        image = jpeg()
        self.path.unlink()
        self.sync([row("H1", "left", image)], [image], OneFace(0))
        self.assertIn("No face", at.problems_for(at.load(self.path), "H1")["left"])


class FakeLink(QObject):
    connectedChanged = pyqtSignal(bool)
    attendanceAck = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.connected, self.sent = True, []
        self.destination = "https://hrm.test"

    def send_text(self, text):
        if self.connected:
            self.sent.append(json.loads(text))
        return self.connected


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if condition():
            return True
        time.sleep(0.005)
    return condition()


class UplinkTests(Base):
    def setUp(self):
        super().setUp()
        self.application = QApplication.instance() or QApplication([])
        self.db = self.root / "attendance.db"
        from face_attendance.config import Config
        self.config = Config(db_path=self.db, hrm_destination="https://hrm.test")
        self.repo = AttendanceRepository(self.config)
        self.addCleanup(self.repo.close)
        self.snapshots = SnapshotService(self.root / "captures")
        self.link = FakeLink()

    def _uplink(self, **kw):
        kw.setdefault('tick_ms', 60_000)
        uplink = hrm_attendance.AttendanceUplink(self.link, lambda: self.config, device_name="Front desk",
                                                 **kw)
        self.addCleanup(uplink.close)
        return uplink

    def _check_in(self, event, code="E1"):
        job = CaptureJob(event, 1, code, "Sok Dara", 1000.0 + len(event), 3.2, np.full((900, 700, 3), 90, np.uint8),
                         match_distance=0.31, live_score=0.98)
        self.repo.cooldown_sec = 0
        self.assertEqual(self.repo.record(job, self.snapshots).outcome, "saved")

    def test_a_check_in_is_sent_with_its_evidence_and_a_small_photo(self):
        self._check_in("e-1")
        self._uplink().kick()
        self.assertTrue(wait_for(lambda: self.link.sent))
        message = self.link.sent[0]
        self.assertEqual((message["type"], message["eventId"], message["employeeId"]),
                         ("attendance.log", "e-1", "E1"))
        self.assertEqual((message["matchDistance"], message["liveScore"], message["deviceName"]),
                         (0.31, 0.98, "Front desk"))
        import base64
        image = cv2.imdecode(np.frombuffer(base64.b64decode(message["snapshotJpeg"]), np.uint8), cv2.IMREAD_COLOR)
        self.assertLessEqual(max(image.shape[:2]), hrm_attendance.PHOTO_MAX_SIDE)

    def test_only_an_acknowledged_check_in_leaves_the_outbox(self):
        self._check_in("e-1")
        self._uplink().kick()
        wait_for(lambda: self.link.sent)
        self.assertEqual(len(self.repo.pending_outbox()), 1)
        self.link.attendanceAck.emit("e-1", "saved")
        self.assertTrue(wait_for(lambda: not self.repo.pending_outbox()))

    def test_a_retry_answer_stays_and_is_sent_again(self):
        self._check_in("e-1")
        uplink = self._uplink()
        uplink.kick()
        wait_for(lambda: self.link.sent)
        self.link.attendanceAck.emit("e-1", "retry")
        wait_for(lambda: not uplink._in_flight)
        self.assertEqual(len(self.repo.pending_outbox()), 1)
        uplink.kick()
        self.assertTrue(wait_for(lambda: len(self.link.sent) == 2))

    def test_offline_check_ins_wait_and_go_after_reconnecting(self):
        self.link.connected = False
        uplink = self._uplink()
        self._check_in("e-1")
        uplink.kick()
        wait_for(lambda: False, 0.1)
        self.assertEqual(self.link.sent, [])
        self.link.connected = True
        self.link.connectedChanged.emit(True)
        self.assertTrue(wait_for(lambda: self.link.sent))

    def test_a_lost_connection_sends_unanswered_ones_again(self):
        self._check_in("e-1")
        uplink = self._uplink()
        uplink.kick()
        wait_for(lambda: self.link.sent)
        self.link.connected = False
        self.link.connectedChanged.emit(False)
        self.link.connected = True
        self.link.connectedChanged.emit(True)
        self.assertTrue(wait_for(lambda: len(self.link.sent) == 2))
        self.assertEqual(self.link.sent[1]["eventId"], "e-1")

    def test_the_queue_empties_batch_after_batch(self):
        for index in range(hrm_attendance.BATCH_SIZE + 3):
            self._check_in(f"e-{index}")
        self._uplink().kick()
        acknowledged = set()

        def acknowledge():
            for message in self.link.sent:
                if message["eventId"] not in acknowledged:
                    acknowledged.add(message["eventId"])
                    self.link.attendanceAck.emit(message["eventId"], "saved")
            return not self.repo.pending_outbox()
        self.assertTrue(wait_for(acknowledge, 10))
        self.assertEqual(len(acknowledged), hrm_attendance.BATCH_SIZE + 3)

    def test_a_database_failure_doesnt_stop_later_attempts(self):
        attempts = []

        def factory(config):
            attempts.append(config)
            if len(attempts) == 1:
                raise OSError("database locked")
            return AttendanceRepository(config)
        self._check_in("e-1")
        uplink = self._uplink(repository_factory=factory)
        uplink.kick()
        self.assertTrue(wait_for(lambda: not uplink._reading))
        uplink.kick()
        self.assertTrue(wait_for(lambda: self.link.sent))

    def test_a_missing_photo_doesnt_stop_the_check_in(self):
        self.assertIsNone(hrm_attendance.photo_text(self.root / "gone.jpg"))
        self.assertIsNone(hrm_attendance.photo_text(""))
        text = hrm_attendance.build_message({"eventId": "e", "employeeId": "E1"}, "PC")
        self.assertIsNone(json.loads(text)["snapshotJpeg"])

    def test_the_ack_is_told_apart_from_other_socket_messages(self):
        self.assertEqual(hrm_live.parse_ack('{"type":"attendance.ack","eventId":"e","result":"saved"}'), ("e", "saved"))
        self.assertIsNone(hrm_live.parse_ack('{"type":"employee.changed","employeeCode":"E"}'))
        self.assertIsNone(hrm_live.parse_ack("not json"))
        self.assertIsNone(hrm_live.parse_ack('{"type":"attendance.ack","eventId":5,"result":"saved"}'))


if __name__ == "__main__":
    unittest.main()
