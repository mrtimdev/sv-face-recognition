import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import numpy as np

import enroll_faces
from face_attendance.catalog import FaceCatalog


class EnrollmentTests(unittest.TestCase):
    def test_edit_name_keeps_identity_samples_and_aliases(self):
        from face_attendance.enrollment import EnrollmentService, read_encodings, write_encodings
        from face_attendance.catalog import load_employee_map, legacy_employee_id
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            faces, employees = root / 'faces.pickle', root / 'employees.json'
            write_encodings(faces, {'Original': [np.ones(128)], 'Alias': [np.ones(128)]})
            service = EnrollmentService(faces, employees)
            before = faces.read_bytes()
            employee_id = service.update_employee('Original', 'New display name')
            self.assertEqual(employee_id, legacy_employee_id('Original'))
            service.update_employee('Original', 'Final display name')
            self.assertEqual(load_employee_map(employees)['Original'].employee_id, employee_id)
            self.assertEqual(load_employee_map(employees)['Original'].name, 'Final display name')
            self.assertEqual(faces.read_bytes(), before)
            with self.assertRaises(ValueError):
                service.update_employee('Original', '  ')
            with self.assertRaises(ValueError):
                service.update_employee('Unknown', 'Name')

    def test_edit_updates_display_name_for_all_aliases_of_same_employee(self):
        from face_attendance.enrollment import EnrollmentService, write_encodings, write_employee
        from face_attendance.catalog import load_employee_map
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            faces, employees = root / 'faces.pickle', root / 'employees.json'
            write_encodings(faces, {'First': [], 'Alias': [], 'Other': []})
            for label, employee_id in [('First', 'E1'), ('Alias', 'E1'), ('Other', 'E2')]:
                write_employee(employees, label, employee_id)
            EnrollmentService(faces, employees).update_employee('Alias', 'Updated')
            records = load_employee_map(employees)
            self.assertEqual(records['First'].name, 'Updated')
            self.assertEqual(records['Alias'].name, 'Updated')
            self.assertEqual(records['Other'].name, 'Other')

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
                patch.object(enroll_faces, "get_backend", return_value=Mock(
                    face_locations=Mock(return_value=[(0, 10, 10, 0)] * 2))), \
                patch.object(enroll_faces, "save_encodings") as save:
            enroll_faces.enroll("Test", "test.jpg")
            save.assert_not_called()


class ConcurrencyProbe:
    """Counts how many backend calls run at the same moment.

    OpenCV networks mutate internal buffers during inference. The lock must
    reduce every caller's overlap to exactly one.
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

    def face_landmarks(self, image, boxes, model):
        self._enter()
        time.sleep(0.03)
        self._leave()
        return [{} for _ in boxes]

    def face_distance(self, known, candidate):
        self._enter()
        time.sleep(0.03)
        self._leave()
        return np.full(len(known), 0.3)


class BackendLockTests(unittest.TestCase):
    FRAME = np.zeros((240, 320, 3), np.uint8)

    def _run_threads(self, target, count=4):
        errors = []
        def run():
            try:
                target()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [], "backend worker raised an exception")

    def test_enrollment_detection_is_serialized(self):
        from face_attendance.enrollment import detect_faces
        probe = ConcurrencyProbe()
        self._run_threads(lambda: detect_faces(probe, self.FRAME))
        self.assertEqual(probe.peak, 1, "detect_faces ran concurrently on the backend")

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
                         "recognition worker and enrollment overlapped on the backend")
