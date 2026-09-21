import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import enroll_faces
from face_attendance.catalog import FaceCatalog


class EnrollmentTests(unittest.TestCase):
    def test_existing_format_and_alias_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(enroll_faces, "ENCODINGS_PATH", str(path / "faces.pickle")), \
                    patch.object(enroll_faces, "EMPLOYEES_PATH", path / "employees.json"):
                enroll_faces.save_encodings({"Old Name": [np.zeros(128)], "Canonical": [np.zeros(128)]})
                enroll_faces.save_employee("Old Name", "E1")
                enroll_faces.save_employee("Canonical", "E1")
                catalog = FaceCatalog(path / "faces.pickle", path / "employees.json")
                self.assertEqual(catalog.enrolled_count, 1)
                self.assertEqual(catalog.duplicate_identities(), [])
                with self.assertRaises(ValueError):
                    enroll_faces.save_employee("Old Name", "E2")

    def test_multiple_faces_are_not_silently_enrolled_as_first_face(self):
        with patch.object(enroll_faces.cv2, "imread", return_value=np.zeros((20, 20, 3), np.uint8)), \
                patch.object(enroll_faces.face_recognition, "face_locations", return_value=[(0, 10, 10, 0)] * 2), \
                patch.object(enroll_faces, "save_encodings") as save:
            enroll_faces.enroll("Test", "test.jpg")
            save.assert_not_called()


class ConcurrencyProbe:
    """Counts how many backend calls run at the same moment.

    ``face_recognition`` wraps ONE global dlib detector, which is not
    thread-safe: concurrent use segfaults the process (seen live when clicking
    Capture while the recognition worker was detecting). The lock must reduce
    every caller's overlap to exactly one.
    """

    def __init__(self):
        self._guard = threading.Lock()
        self.inside = 0
        self.peak = 0

    def _enter(self):
        with self._guard:
            self.inside += 1
            self.peak = max(self.peak, self.inside)

    def _leave(self):
        with self._guard:
            self.inside -= 1

    def face_locations(self, image, model):
        self._enter()
        time.sleep(0.03)
        self._leave()
        return [(0, 10, 10, 0)]

    def face_encodings(self, image, boxes):
        self._enter()
        time.sleep(0.03)
        self._leave()
        return [np.zeros(128) for _ in boxes]

    def face_distance(self, known, candidate):
        self._enter()
        time.sleep(0.03)
        self._leave()
        return np.array([0.3])


class BackendLockTests(unittest.TestCase):
    FRAME = np.zeros((240, 320, 3), np.uint8)

    def _run_threads(self, target, count=4):
        threads = [threading.Thread(target=target) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def test_enrollment_detection_is_serialized(self):
        from face_attendance.enrollment import detect_faces
        probe = ConcurrencyProbe()
        self._run_threads(lambda: detect_faces(probe, self.FRAME))
        self.assertEqual(probe.peak, 1, "detect_faces ran concurrently on dlib")

    def test_recognition_backend_calls_are_serialized(self):
        from types import SimpleNamespace

        from face_attendance.config import Config
        from face_attendance.models import Employee, FramePacket, RecognitionRequest
        from face_attendance.recognition import RecognitionService

        probe = ConcurrencyProbe()
        catalog = SimpleNamespace(employees=(Employee("A", "Alice"), Employee("A", "Alice")),
                                  encodings=np.zeros((2, 128)))
        service = RecognitionService(Config(), catalog, probe)
        packet = FramePacket(100, 10, 1000, 1, self.FRAME)
        self._run_threads(lambda: service.process(RecognitionRequest(packet, ())))
        self.assertEqual(probe.peak, 1,
                         "recognition worker and enrollment overlapped on dlib")
