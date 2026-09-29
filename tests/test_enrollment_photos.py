import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from face_attendance.enrollment import EnrollmentService, read_encodings, write_encodings
from face_attendance.enrollment_photos import photo_directory, sample_photos


class PhotoBackend:
    def face_locations(self, image, model=None):
        return [] if int(image[0, 0, 0]) == 0 else [(10, 150, 150, 10)]

    def face_encodings(self, image, boxes):
        vector = np.ones(128, np.float32)
        vector[0] = float(image[0, 0, 0])
        return [vector for _ in boxes]


def frame(value):
    return np.full((160, 160, 3), value, np.uint8)


class EnrollmentPhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.catalog = self.root / 'faces.pickle'
        self.service = EnrollmentService(self.catalog, self.root / 'employees.json',
                                         backend=PhotoBackend(), check_quality=False)

    def test_each_accepted_sample_has_its_own_photo_across_batches_and_reload(self):
        outcome = self.service.enroll_many([frame(60), frame(140)], 'Alice', 'E1')
        self.assertTrue(outcome.ok)
        first = sample_photos(self.catalog, 'Alice')
        self.assertEqual(len(first), 2)
        self.assertNotEqual(first[0], first[1])
        self.assertEqual([int(cv2.imread(str(p))[0, 0, 0]) for p in first], [60, 140])
        self.service.enroll(frame(200), 'Alice', 'E1')
        saved = sample_photos(self.catalog, 'Alice')
        self.assertEqual(saved[:2], first)
        self.assertEqual(len(saved), 3)
        self.assertEqual(len(read_encodings(self.catalog)['Alice']), 3)
        self.assertEqual(tuple(map(str, first)), outcome.photo_paths)

    def test_rejected_first_image_never_becomes_a_sample_photo(self):
        outcome = self.service.enroll_many([frame(0), frame(70), frame(150)], 'Alice')
        self.assertEqual(outcome.samples_added, 2)
        self.assertTrue(outcome.issues)
        photos = sample_photos(self.catalog, 'Alice')
        self.assertEqual([int(cv2.imread(str(p))[0, 0, 0]) for p in photos], [70, 150])
        self.assertEqual(len(list(photo_directory(self.catalog).glob('*.jpg'))), 2)

    def test_old_encodings_remain_and_missing_photos_are_not_invented(self):
        original = [np.ones(128), np.ones(128) * 2]
        write_encodings(self.catalog, {'Alice': original})
        self.assertEqual(sample_photos(self.catalog, 'Alice'), [None, None])
        self.service.enroll(frame(70), 'Alice')
        photos = sample_photos(self.catalog, 'Alice')
        self.assertEqual(photos[:2], [None, None])
        self.assertTrue(photos[2].is_file())
        for expected, actual in zip(original, read_encodings(self.catalog)['Alice']):
            np.testing.assert_array_equal(expected, actual)

    def test_delete_sample_and_employee_remove_only_their_linked_photos(self):
        self.service.enroll_many([frame(60), frame(140)], 'Alice')
        self.service.enroll(frame(160), 'Bob')
        a, b = sample_photos(self.catalog, 'Alice'), sample_photos(self.catalog, 'Bob')
        self.assertEqual(self.service.delete_sample('Alice', 0), 1)
        self.assertFalse(a[0].exists())
        self.assertEqual(sample_photos(self.catalog, 'Alice'), [a[1]])
        self.service.delete_employee('Alice')
        self.assertFalse(a[1].exists())
        self.assertTrue(b[0].exists())
        self.assertEqual(len(read_encodings(self.catalog)['Bob']), 1)

    def test_failed_catalog_commit_removes_new_photos_and_preserves_old_enrollment(self):
        self.service.enroll(frame(70), 'Alice')
        original = self.catalog.read_bytes()
        photos = sample_photos(self.catalog, 'Alice')
        with patch('face_attendance.enrollment.write_encodings', side_effect=OSError('disk full')):
            outcome = self.service.enroll_many([frame(80), frame(90)], 'Alice')
        self.assertFalse(outcome.ok)
        self.assertEqual(self.catalog.read_bytes(), original)
        self.assertEqual(list(photo_directory(self.catalog).glob('*.jpg')), photos)

    def test_failure_writing_second_photo_does_not_add_any_samples(self):
        from face_attendance.enrollment_photos import save_sample_photo
        calls = []
        def save(path, image):
            calls.append(True)
            if len(calls) == 2:
                raise OSError('photo failure')
            return save_sample_photo(path, image)
        with patch('face_attendance.enrollment.save_sample_photo', side_effect=save):
            outcome = self.service.enroll_many([frame(80), frame(90)], 'Alice')
        self.assertFalse(outcome.ok)
        self.assertEqual(read_encodings(self.catalog), {})
        self.assertEqual(list(photo_directory(self.catalog).glob('*.jpg')), [])

    def test_legacy_writer_preserves_photo_links_for_unchanged_samples(self):
        self.service.enroll_many([frame(60), frame(140)], 'Alice')
        photos = sample_photos(self.catalog, 'Alice')
        vectors = read_encodings(self.catalog)
        vectors['Alice'].reverse()
        write_encodings(self.catalog, vectors)
        self.assertEqual(sample_photos(self.catalog, 'Alice'), list(reversed(photos)))

    def test_cli_saves_original_photo_with_the_sample(self):
        import enroll_faces
        with patch.object(enroll_faces, 'ENCODINGS_PATH', str(self.catalog)), \
                patch.object(enroll_faces, 'EMPLOYEES_PATH', self.root / 'employees.json'), \
                patch.object(enroll_faces, 'get_backend', return_value=PhotoBackend()), \
                patch.object(enroll_faces.cv2, 'imread', return_value=frame(80)):
            enroll_faces.enroll('Alice', 'input.jpg')
        self.assertEqual(len(sample_photos(self.catalog, 'Alice')), 1)
        self.assertTrue(sample_photos(self.catalog, 'Alice')[0].exists())


class EnrollmentGalleryTests(unittest.TestCase):
    def test_profile_shows_every_sample_and_activity_opens_all_saved_photos(self):
        from tests.test_dashboard import _app, temp_settings
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.dashboard.widgets.activity_detail import enrollment_photos
        from PyQt6.QtWidgets import QLabel
        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            settings = temp_settings(directory)
            service = EnrollmentService(settings.encodings_path, settings.employees_path,
                                         backend=PhotoBackend(), check_quality=False)
            service.enroll_many([frame(80), frame(150)], 'Seed')
            window = MainWindow(settings, use_lock=False)
            window.show()
            screen = window.screens[2]
            try:
                screen.employee_table.selectRow(0)
                screen._show_profile()
                app.processEvents()
                row = screen.rows[0]
                self.assertEqual(screen.enrolled_count_label.text(), '3 enrolled · 2 photos')
                photos = [label for label in screen.enrolled_container.findChildren(QLabel)
                          if label.accessibleName().startswith('Enrolled sample')]
                self.assertEqual(len(photos), 3)
                self.assertEqual(photos[0].text(), 'Photo\nunavailable')
                self.assertFalse(photos[1].pixmap().isNull())
                self.assertFalse(photos[2].pixmap().isNull())
                self.assertEqual(enrollment_photos(settings, row['employee_id'], screen.rows),
                                 sample_photos(settings.encodings_path, 'Seed')[1:])
                with patch.object(screen, '_open_image') as opened:
                    photos[2].mousePressEvent(None)
                    opened.assert_called_once_with(sample_photos(settings.encodings_path, 'Seed')[2])
                self.assertIn('matching', screen.enrolled_hint.text())
                self.assertEqual(screen.gallery_count_label.text(), 'none')  # Attendance is separate.
            finally:
                window.close()
                app.processEvents()
