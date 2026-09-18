import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from face_attendance.camera import CameraManager
from face_attendance.channels import LatestValue
from face_attendance.config import Config
from face_attendance.models import (CaptureJob, Employee, FramePacket, RecognitionRequest,
                                    RecognitionResult, TrackHint)
from face_attendance.persistence import PersistenceWorker
from face_attendance.recognition import RecognitionService, RecognitionWorker


class FakeBackend:
    def __init__(self):
        self.encoded = 0
        self.distance_calls = 0
        self.boxes = [(10, 50, 50, 10)]
        self.distances = np.array([0.3, 0.4, 0.7])

    def face_locations(self, image, model):
        return self.boxes

    def face_encodings(self, image, boxes):
        self.encoded += len(boxes)
        return [np.zeros(128) for _ in boxes]

    def face_distance(self, known, candidate):
        self.distance_calls += 1
        return self.distances


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.a, self.b = Employee("A", "Alice"), Employee("B", "Bob")
        self.catalog = SimpleNamespace(employees=(self.a, self.a, self.b), encodings=np.zeros((3, 128)))
        self.backend = FakeBackend()
        self.service = RecognitionService(self.cfg, self.catalog, self.backend)
        self.packet = FramePacket(100, 10, 1000, 1, np.zeros((240, 320, 3), np.uint8))

    def test_match_computes_distances_once_and_uses_employee_margin(self):
        employee, distance = self.service.match(np.zeros(128))
        self.assertEqual(employee, self.a)
        self.assertEqual(distance, .3)
        self.assertEqual(self.backend.distance_calls, 1)
        self.backend.distances = np.array([.3, .31, .32])
        self.assertIsNone(self.service.match(np.zeros(128))[0])
        self.backend.distances = np.array([.6, .7, .8])
        self.assertIsNone(self.service.match(np.zeros(128))[0])

    def test_stable_identity_reuses_encoding_then_rechecks(self):
        hint = TrackHint(1, (40, 200, 200, 40), "A", 9.8, 94, True)
        result = self.service.process(RecognitionRequest(self.packet, (hint,)))
        self.assertFalse(result.detections[0].encoded)
        self.assertEqual(self.backend.encoded, 0)
        result = self.service.process(RecognitionRequest(replace(self.packet, captured_at=10.6), (hint,)))
        self.assertTrue(result.detections[0].encoded)
        self.assertEqual(result.detections[0].employee, self.a)

    def test_new_faces_encode_and_scaling_uses_actual_dimensions(self):
        odd = replace(self.packet, frame=np.zeros((479, 641, 3), np.uint8))
        result = self.service.process(RecognitionRequest(odd, ()))
        self.assertTrue(result.detections[0].encoded)
        self.assertAlmostEqual(result.detections[0].bounding_box[1], 50 * 641 / 160)

    def test_latest_request_replaces_backlog(self):
        entered, release, latest_done = threading.Event(), threading.Event(), threading.Event()
        processed = []

        class SlowService:
            config = Config()

            def process(self, request):
                processed.append(request.packet.sequence)
                if len(processed) == 1:
                    entered.set()
                    release.wait(2)
                else:
                    latest_done.set()
                return RecognitionResult(request.packet, (), 0)

        worker = RecognitionWorker(SlowService())
        worker.start()
        try:
            packet = replace(self.packet, captured_at=time.monotonic(), sequence=1)
            worker.requests.put(RecognitionRequest(packet, ()))
            self.assertTrue(entered.wait(1))
            for seq in range(2, 21):
                worker.requests.put(RecognitionRequest(replace(packet, sequence=seq), ()))
            release.set()
            self.assertTrue(latest_done.wait(1))
            self.assertEqual(processed, [1, 20])
        finally:
            release.set()
            worker.close()
        self.assertFalse(worker.thread.is_alive())


class CameraTests(unittest.TestCase):
    def test_startup_failure_disconnect_reconnect_and_shutdown(self):
        instances, statuses = [], []
        connected_twice = threading.Event()

        class Capture:
            def __init__(self, index):
                self.index, self.reads, self.released = index, 0, False

            def isOpened(self):
                return self.index != 0

            def read(self):
                self.reads += 1
                if self.index == 1 and self.reads > 1:
                    return False, None
                if self.index >= 2:
                    connected_twice.set()
                    threading.Event().wait(.005)
                return True, np.zeros((24, 32, 3), np.uint8)

            def release(self):
                self.released = True

        def factory():
            cap = Capture(len(instances))
            instances.append(cap)
            return cap

        manager = CameraManager(replace(Config(), reconnect_sec=.01), factory)
        original = manager._set_status

        def record_status(status):
            statuses.append(status)
            original(status)

        manager._set_status = record_status
        manager.start()
        try:
            self.assertTrue(connected_twice.wait(2))
        finally:
            manager.close()
        self.assertIn("CAMERA DISCONNECTED", statuses)
        self.assertIn("RECONNECTING", statuses)
        self.assertTrue(all(cap.released for cap in instances))
        self.assertFalse(manager.thread.is_alive())
        self.assertEqual(manager.frames.get().generation, 2)

    def test_camera_stall_is_reported_before_read_returns(self):
        manager = CameraManager(Config())
        manager.frames.put(FramePacket(1, 10, 1000, 1, np.zeros((2, 2, 3))))
        manager._set_status("CONNECTED")
        self.assertEqual(manager.status(10.5), "CONNECTED")
        self.assertEqual(manager.status(12), "RECONNECTING")

    def test_mailbox_contains_only_newest_frame(self):
        mailbox = LatestValue()
        for number in range(1000):
            mailbox.put(number)
        self.assertEqual(mailbox.take(), 999)
        self.assertIsNone(mailbox.take())


class PersistenceTests(unittest.TestCase):
    def test_storage_runs_off_thread_and_drains_on_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = replace(Config(), db_path=root / "test.db", capture_dir=root / "captures",
                          alert_path=root / "missing.wav", log_path=root / "events.csv")
            worker = PersistenceWorker(cfg, {})
            worker.start()
            self.assertTrue(worker.startup.ready.wait(3))
            self.assertEqual(worker.startup.take(), ({}, ""))
            for index in range(3):
                self.assertTrue(worker.submit(CaptureJob(str(index), index, str(index), "Employee",
                                                         time.time(), 3, np.zeros((20, 20, 3), np.uint8))))
            worker.close()
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(worker.results.qsize(), 3)
            self.assertTrue(all(worker.results.get_nowait().outcome == "saved" for _ in range(3)))
            self.assertEqual(len(list((root / "captures").glob("*.jpg"))), 3)
