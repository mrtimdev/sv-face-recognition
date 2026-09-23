import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import cv2
import numpy as np

from face_attendance.catalog import FaceCatalog
from face_attendance.enrollment import read_encodings, write_encodings
from face_attendance.face_backend import MODEL_ID, OpenCVFaceBackend
from face_attendance.migration import migrate_photos
from face_attendance.settings import Settings
from face_attendance.template_store import template_payload


class BackendTests(unittest.TestCase):
    def test_cosine_distance_is_scale_invariant_and_rejects_invalid_vectors(self):
        candidate = np.eye(1, 128)[0]
        known = np.array([candidate * 3, -candidate, np.roll(candidate, 1),
                          np.zeros(128), np.full(128, np.nan)])
        distances = OpenCVFaceBackend.face_distance(known, candidate)
        np.testing.assert_allclose(distances[:3], [0, 2, 1])
        self.assertTrue(np.isinf(distances[3:]).all())
        self.assertTrue(np.isinf(OpenCVFaceBackend.face_distance(known, candidate * 0)).all())

    def test_bundled_models_load_and_blank_frame_has_no_face(self):
        backend = OpenCVFaceBackend()
        self.assertEqual(backend.face_locations(np.zeros((240, 320, 3), np.uint8)), [])
        backend.promote(np.zeros((720, 1280, 3), np.uint8), 4, 3)
        self.assertEqual(backend._rows.shape, (0, 15))

    def test_full_resolution_promotion_scales_box_and_alignment_points(self):
        backend = OpenCVFaceBackend.__new__(OpenCVFaceBackend)
        backend._rows = np.array([[10, 20, 30, 40, 15, 25, 35, 25,
                                   25, 35, 15, 45, 35, 45, .95]], np.float32)
        image = np.zeros((240, 320, 3), np.uint8)
        backend.promote(image, 4, 3)
        np.testing.assert_allclose(backend._row(image, (60, 160, 180, 40)),
                                   [40, 60, 120, 120, 60, 75, 140, 75,
                                    100, 105, 60, 135, 140, 135, .95])

    def test_clipped_face_cannot_calibrate_expression_baseline(self):
        backend = OpenCVFaceBackend.__new__(OpenCVFaceBackend)
        backend._image = np.zeros((240, 320, 3), np.uint8)
        backend._rows = np.array([[-5, 20, 150, 200] + [0] * 10 + [.99]], np.float32)
        # No mesh is loaded: incomplete faces must be rejected before inference.
        self.assertEqual(backend.face_landmarks(backend._image, [(20, 145, 220, 0)]), [{}])


class TemplateTests(unittest.TestCase):
    def test_legacy_and_wrong_model_vectors_are_rejected_even_with_128_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'faces.pickle'
            for payload in ({'Alice': [np.ones(128)]},
                            dict(template_payload({'Alice': [np.ones(128)]}), model='dlib')):
                path.write_bytes(pickle.dumps(payload))
                with self.assertRaisesRegex(ValueError, 'Incompatible face templates'):
                    read_encodings(path)

    def test_round_trip_retains_employee_identity_and_empty_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'employees.json').write_text('{"Alice":{"employee_id":"EMP-1","name":"Alice"}}')
            write_encodings(root / 'faces.pickle', {'Alice': [np.ones(128)], 'Needs photo': []})
            catalog = FaceCatalog(root / 'faces.pickle', root / 'employees.json')
            self.assertEqual(catalog.employees[0].employee_id, 'EMP-1')
            self.assertIn('Needs photo', read_encodings(root / 'faces.pickle'))

    def test_legacy_settings_redirect_templates_and_reset_old_distance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            original = '{"encodings_path":"staff.pickle","face_tolerance":0.67}'
            path.write_text(original)
            settings = Settings.load(path)
            self.assertEqual(settings.encodings_path, 'staff_sface.pickle')
            self.assertEqual(settings.face_tolerance, .5)
            self.assertEqual(settings.recognition_backend, MODEL_ID)
            self.assertEqual(path.read_text(), original)
            settings.save(path)
            self.assertEqual(Settings.load(path).encodings_path, 'staff_sface.pickle')


class MigrationTests(unittest.TestCase):
    def test_migration_requires_unique_exact_photo_and_preserves_originals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, output, employees = root / 'old.pickle', root / 'new.pickle', root / 'employees.json'
            labels = ['Good', 'Missing', 'Two Faces', 'A B', 'A_B']
            original = pickle.dumps({name: [np.zeros(128)] for name in labels})
            old.write_bytes(original)
            employees.write_text('{"Good":{"employee_id":"EMP-1","name":"Good"}}')
            mapping = employees.read_bytes()
            photos = root / 'enrollment_photos'
            photos.mkdir()
            image = np.random.default_rng(1).integers(60, 200, (160, 160, 3), dtype=np.uint8)
            for name in ['Good', 'Two_Faces', 'A_B', 'Missing_similar']:
                cv2.imwrite(str(photos / (name + '.jpg')), image)
            backend = Mock()
            backend.face_locations.side_effect = [[(10, 150, 150, 10)], [(10, 150, 150, 10)] * 2]
            backend.face_encodings.return_value = [np.ones(128)]
            report = migrate_photos(old, output, employees, backend=backend)
            data = read_encodings(output)
            self.assertEqual([name for name, samples in data.items() if samples], ['Good'])
            self.assertIn('ambiguous', report['A B'])
            self.assertIn('Re-enroll', report['Two Faces'])
            self.assertEqual(old.read_bytes(), original)
            self.assertEqual(employees.read_bytes(), mapping)
            self.assertEqual(FaceCatalog(output, employees).employees[0].employee_id, 'EMP-1')
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                migrate_photos(old, output, employees, backend=backend)
            self.assertEqual(len(read_encodings(output)['Good']), 1)


if __name__ == '__main__':
    unittest.main()
