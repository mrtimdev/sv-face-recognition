import tempfile
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
