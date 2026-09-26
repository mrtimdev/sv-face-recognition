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
from face_attendance.liveness import LivenessChecker
from face_attendance.runtime import AttendanceApplication
from tests.test_attendance import FakePersistence
from tests.test_liveness import landmarks, OPEN_EYE, CLOSED_EYE, response_for


class FakeAntiSpoof:
    """Explicit PAD result for pipeline fixtures; never used by the application."""
    def __init__(self, score=.99):
        self.score = score

    def evaluate(self, frame, box):
        return self.score, ""


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


class BlinkBackend(Backend):
    now = 0

    def face_landmarks(self, image, boxes, model):
        phase = self.now % 1.2
        eye = CLOSED_EYE if .25 <= phase < .45 or .8 <= phase < 1.0 else OPEN_EYE
        return [landmarks(eye) for _ in boxes]


class ExpressionBackend(Backend):
    checker = None

    def face_landmarks(self, image, boxes, model):
        return [response_for(self.checker, index + 1) for index in range(len(boxes))]


class IntegrationTests(unittest.TestCase):
    def test_brief_flow_interruptions_resume_and_capture_once(self):
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"),), encodings=np.zeros((1, 128)))
        config = Config()
        tracker, backend, worker = FaceTracker(config), ExpressionBackend(), FakePersistence()
        backend.faces, backend.checker = 1, tracker.liveness
        recognition = RecognitionService(config, catalog, backend, anti_spoof=FakeAntiSpoof())
        service = AttendanceService(config, worker)
        worker.startup.put(({}, ""))
        saved_at = []
        for seq in range(210):
            now = 10 + seq / 30
            interrupted = seq > 15 and seq % 15 == 7
            if interrupted:
                tracker.tracks[1].points = None
                before = tracker.tracks[1].verified_presence
            packet = FramePacket(seq, now, 1000 + now, 1, frame)
            tracker.advance(packet)
            if seq % config.detection_interval == 0:
                tracker.apply(recognition.process(RecognitionRequest(packet, tracker.hints())), now)
            service.poll(tracker.tracks, now, packet.wall_time)
            if interrupted and not saved_at:
                self.assertEqual(tracker.tracks[1].verified_presence, before)
            jobs = service.update(tracker.tracks, packet, now, True)
            if interrupted:
                self.assertEqual(jobs, [])
            for job in jobs:
                saved_at.append(now)
                worker.results.put(SaveResult(job, "saved", job.captured_at))
        self.assertEqual(len(saved_at), 1)
        self.assertGreaterEqual(worker.jobs[0].duration, config.capture_after_sec)
        self.assertLess(saved_at[0], 16, "Brief tracking interruptions stalled check-in")

    def test_intermittent_false_live_bursts_cannot_bank_attendance(self):
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"),), encodings=np.zeros((1, 128)))
        for mode in ("face", "blink"):
            with self.subTest(mode=mode):
                config = replace(Config(), attendance_mode=mode, stable_recheck_sec=1.0)
                tracker, backend, worker, pad = FaceTracker(config), ExpressionBackend(), FakePersistence(), FakeAntiSpoof()
                backend.faces, backend.checker = 1, tracker.liveness
                recognition = RecognitionService(config, catalog, backend, anti_spoof=pad)
                service = AttendanceService(config, worker)
                worker.startup.put(({}, ""))
                for seq in range(150):
                    now = 10 + seq * .1
                    # Up to 0.9 seconds of false-live scores after every rejection.
                    # After eight seconds, simulate a genuinely sustained live face.
                    pad.score = .01 if seq <= 80 and seq % 10 == 0 else .99
                    packet = FramePacket(seq * 3, now, 1000 + now, 1, frame)
                    tracker.advance(packet)
                    tracker.apply(recognition.process(RecognitionRequest(packet, tracker.hints())), now)
                    service.poll(tracker.tracks, now, packet.wall_time)
                    service.update(tracker.tracks, packet, now, True)
                    if seq < 80:
                        self.assertEqual(worker.jobs, [])
                self.assertEqual(len(worker.jobs), 1)
                self.assertGreaterEqual(worker.jobs[0].captured_at, 1021.1)

    def test_selected_requirements_gate_actual_capture_jobs(self):
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"),), encodings=np.zeros((1, 128)))
        cases = (("face", "none", .99, True), ("face", "none", .01, False),
                 ("blink", "smile", .99, False), ("smile", "blink", .99, False),
                 ("blink_and_smile", "blink", .99, False),
                 ("blink_and_smile", "smile", .99, False),
                 ("blink", "blink", .99, True), ("smile", "smile", .99, True),
                 ("blink_and_smile", "both", .99, True))
        for mode, action, score, expected in cases:
            with self.subTest(mode=mode, action=action, score=score):
                config = replace(Config(), attendance_mode=mode, capture_after_sec=.5)
                tracker, backend, worker = FaceTracker(config), Backend(), FakePersistence()
                backend.faces = 1
                landmark_calls = []

                def expressions(image, boxes, model):
                    landmark_calls.append(True)
                    state = tracker.liveness._state.get(1, {})
                    ready = state.get("phase") == "ready"
                    if action in ("blink", "both") and not state.get("blink_done"):
                        return [response_for(tracker.liveness)]
                    return [landmarks(smile=ready and action in ("smile", "both"))]

                backend.face_landmarks = expressions
                recognition = RecognitionService(config, catalog, backend, anti_spoof=FakeAntiSpoof(score))
                service = AttendanceService(config, worker)
                worker.startup.put(({}, ""))
                for seq in range(45):
                    now = 10 + seq * .1
                    packet = FramePacket(seq * 3, now, 1000 + now, 1, frame)
                    tracker.advance(packet)
                    tracker.apply(recognition.process(RecognitionRequest(packet, tracker.hints())), now)
                    service.poll(tracker.tracks, now, packet.wall_time)
                    service.update(tracker.tracks, packet, now, True)
                self.assertEqual(len(worker.jobs), int(expected))
                if mode == "face":
                    self.assertEqual(landmark_calls, [])

    def test_main_loop_without_landmarks_blocks_records_and_releases_workers(self):
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
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0], 0)
            finally:
                conn.close()

    def test_two_employees_auto_capture_and_restart_cooldown(self):
        config = Config()
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"), Employee("B", "Bob")),
                                  encodings=np.array([np.zeros(128), np.ones(128)]))
        backend = ExpressionBackend()
        recognition = RecognitionService(config, catalog, backend, anti_spoof=FakeAntiSpoof())
        tracker = FaceTracker(config)
        tracker.liveness = LivenessChecker()
        backend.checker = tracker.liveness
        worker = FakePersistence()
        service = AttendanceService(config, worker)
        worker.startup.put(({}, ""))
        saved_events = []
        for seq in range(240):
            now = 10 + seq / 30
            backend.now = seq / 30
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
        self.assertTrue(all(track.cooldown_remaining > 23 for track in tracker.tracks.values()))
        # Identity encodings remain throttled while verification is pending.
        self.assertLess(backend.encoded, 45)

    def test_smile_without_blink_records_attendance(self):
        def smiling_backend(backend, image, boxes, model):
            return [landmarks(smile=backend.checker.prompt(index + 1) == "Blink once or smile")
                    for index in range(len(boxes))]
        with patch.object(ExpressionBackend, "face_landmarks", smiling_backend):
            self.test_two_employees_auto_capture_and_restart_cooldown()

    def test_early_departure_and_return_require_new_continuous_verification(self):
        config = Config()
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"),), encodings=np.array([np.zeros(128)]))
        backend = ExpressionBackend()
        backend.faces = 1
        recognition, tracker = RecognitionService(config, catalog, backend, anti_spoof=FakeAntiSpoof()), FaceTracker(config)
        tracker.liveness = LivenessChecker()
        backend.checker = tracker.liveness
        worker, submitted = FakePersistence(), []
        service = AttendanceService(config, worker)
        worker.startup.put(({}, ""))
        for seq in range(240):
            now = 10 + seq / 30
            backend.now = seq / 30
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

    def test_static_phone_or_paper_face_never_creates_attendance(self):
        config = Config()
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"),), encodings=np.zeros((1, 128)))
        backend = BlinkBackend()
        backend.faces = 1
        # A high-texture photograph with valid but permanently open eyes.
        backend.now = 0
        recognition, tracker = RecognitionService(config, catalog, backend, anti_spoof=FakeAntiSpoof()), FaceTracker(config)
        worker = FakePersistence()
        service = AttendanceService(config, worker)
        worker.startup.put(({}, ""))
        for seq in range(180):
            now = 10 + seq / 30
            packet = FramePacket(seq, now, 1000 + seq / 30, 1, frame)
            tracker.advance(packet)
            if seq % config.detection_interval == 0:
                tracker.apply(recognition.process(RecognitionRequest(packet, tracker.hints())), now)
            service.poll(tracker.tracks, now, packet.wall_time)
            service.update(tracker.tracks, packet, now, True)
            service.observe(tracker.tracks, now, packet.wall_time)
        self.assertTrue(tracker.tracks[1].identity_valid)
        self.assertGreater(tracker.tracks[1].verified_presence, config.capture_after_sec)
        self.assertEqual(worker.jobs, [])
        self.assertEqual(worker.events, [])

    def test_blinking_replay_rejected_by_pad_never_creates_attendance(self):
        config = Config()
        frame = np.random.default_rng(31).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"),), encodings=np.zeros((1, 128)))
        backend = BlinkBackend()
        backend.faces = 1
        recognition, tracker = RecognitionService(config, catalog, backend, anti_spoof=FakeAntiSpoof(.01)), FaceTracker(config)
        worker = FakePersistence()
        service = AttendanceService(config, worker)
        worker.startup.put(({}, ""))
        for seq in range(240):
            now = 10 + seq / 30
            backend.now = seq / 30
            packet = FramePacket(seq, now, 1000 + seq / 30, 1, frame)
            tracker.advance(packet)
            if seq % config.detection_interval == 0:
                tracker.apply(recognition.process(RecognitionRequest(packet, tracker.hints())), now)
            service.poll(tracker.tracks, now, packet.wall_time)
            service.update(tracker.tracks, packet, now, True)
            service.observe(tracker.tracks, now, packet.wall_time)
        self.assertTrue(tracker.tracks[1].identity_valid)
        self.assertEqual(worker.jobs, [])
        self.assertEqual(worker.events, [])
