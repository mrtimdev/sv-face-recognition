"""Deterministic camera -> recognition -> tracking -> attendance integration."""
import unittest
import sqlite3
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from face_attendance.attendance import AttendanceService
from face_attendance.camera import CameraManager
from face_attendance.config import Config
from face_attendance.models import Employee, FramePacket, RecognitionRequest, SaveResult
from face_attendance.recognition import RecognitionService
from face_attendance.tracking import FaceTracker
from face_attendance.runtime import AttendanceApplication
from tests.test_attendance import FakePersistence


class Backend:
    def __init__(self):
        self.faces = 2
        self.encoded = 0

    def face_locations(self, image, model):
        return [(10, 40, 45, 5), (10, 105, 45, 70)][:self.faces]

    def face_encodings(self, image, boxes):
        self.encoded += len(boxes)
        return [np.ones(128) * (0 if b[3] == 5 else 1) for b in boxes]

    @staticmethod
    def face_distance(known, encoding):
        return np.linalg.norm(known - encoding, axis=1)


class IntegrationTests(unittest.TestCase):
    def test_main_loop_records_then_q_releases_all_workers(self):
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        employees = (Employee("A", "Alice"), Employee("B", "Bob"))
        catalog = SimpleNamespace(employees=employees, encodings=np.array([np.zeros(128), np.ones(128)]),
                                  enrolled_count=2, employee_map={e.name: e for e in employees},
                                  duplicate_identities=lambda: [])

        class Capture:
            released = False

            @staticmethod
            def isOpened():
                return True

            @staticmethod
            def read():
                threading.Event().wait(1 / 60)
                return True, frame

            def release(self):
                self.released = True

        cap, ticks = Capture(), []

        def wait_key(delay):
            ticks.append(1)
            threading.Event().wait(max(.001, delay / 1000))
            return ord("q") if len(ticks) >= 65 else -1

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(Config(), target_fps=60, detection_interval=1, recognition_interval=1,
                             capture_after_sec=.12, stable_recheck_sec=.04, camera_width=480, camera_height=240,
                             db_path=root / "attendance.db", capture_dir=root / "captures",
                             log_path=root / "observations.csv", alert_path=root / "missing.wav")
            with patch("face_attendance.runtime.FaceCatalog", return_value=catalog), \
                    patch("face_attendance.runtime.RecognitionService", side_effect=lambda cfg, cat: RecognitionService(cfg, cat, Backend())), \
                    patch("face_attendance.runtime.CameraManager", side_effect=lambda cfg: CameraManager(cfg, lambda: cap)), \
                    patch("face_attendance.runtime.cv2.namedWindow"), \
                    patch("face_attendance.runtime.cv2.imshow"), \
                    patch("face_attendance.runtime.cv2.waitKey", side_effect=wait_key), \
                    patch("face_attendance.runtime.cv2.getWindowProperty", return_value=1), \
                    patch("face_attendance.runtime.cv2.destroyAllWindows") as destroy:
                app = AttendanceApplication(config)
                app.run()
            self.assertTrue(cap.released)
            self.assertTrue(all(not worker.thread.is_alive() for worker in
                                (app.camera, app.recognition, app.persistence)))
            destroy.assert_called_once()
            conn = sqlite3.connect(config.db_path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0], 2)
            finally:
                conn.close()

    def test_two_employees_auto_capture_and_restart_cooldown(self):
        config = Config()
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"), Employee("B", "Bob")),
                                  encodings=np.array([np.zeros(128), np.ones(128)]))
        backend = Backend()
        recognition = RecognitionService(config, catalog, backend)
        tracker = FaceTracker(config)
        worker = FakePersistence()
        service = AttendanceService(config, worker)
        worker.startup.put(({}, ""))
        saved_events = []
        for seq in range(180):
            now = 10 + seq / 30
            packet = FramePacket(seq, now, 1000 + seq / 30, 1, frame)
            tracker.advance(packet)
            if seq % config.detection_interval == 0:
                result = recognition.process(RecognitionRequest(packet, tracker.hints()))
                self.assertTrue(tracker.apply(result, now))
            saved_events.extend(service.poll(tracker.tracks, now, packet.wall_time))
            for job in service.update(tracker.tracks, packet, now, True):
                self.assertGreaterEqual(job.duration, config.capture_after_sec)
                worker.results.put(SaveResult(job, "saved", packet.wall_time))
        self.assertEqual(len(worker.jobs), 2)
        self.assertEqual({job.employee_id for job in worker.jobs}, {"A", "B"})
        self.assertEqual(len(saved_events), 2)
        self.assertTrue(all(track.cooldown_remaining > 25 for track in tracker.tracks.values()))
        # Detection happened 60 times per face, but each face was encoded fewer than 15 times.
        self.assertLess(backend.encoded, 30)

    def test_early_departure_and_return_require_new_continuous_verification(self):
        config = Config()
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"),), encodings=np.array([np.zeros(128)]))
        backend = Backend()
        backend.faces = 1
        recognition, tracker = RecognitionService(config, catalog, backend), FaceTracker(config)
        worker, submitted = FakePersistence(), []
        service = AttendanceService(config, worker)
        worker.startup.put(({}, ""))
        for seq in range(180):
            now = 10 + seq / 30
            backend.faces = 0 if 60 <= seq < 75 else 1
            packet = FramePacket(seq, now, 1000 + seq / 30, 1, frame)
            tracker.advance(packet)
            if seq % config.detection_interval == 0:
                tracker.apply(recognition.process(RecognitionRequest(packet, tracker.hints())), now)
            service.poll(tracker.tracks, now, packet.wall_time)
            if service.update(tracker.tracks, packet, now, True):
                submitted.append(now)
        self.assertEqual(len(submitted), 1)
        self.assertGreaterEqual(submitted[0], 15.5)
