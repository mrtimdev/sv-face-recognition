"""Event-specific photos, exact enrollment resolution and modal lifecycle."""
import hashlib
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from tests.test_dashboard import _app, temp_settings


def photo_path(root, label):
    folder = Path(root) / 'enrollment_photos'
    folder.mkdir(exist_ok=True)
    safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in label)[:60]
    return folder / f'{safe}_{hashlib.sha256(label.encode()).hexdigest()[:12]}.jpg'


class ActivityDetailTests(unittest.TestCase):
    def test_face_crop_is_default_and_full_frame_is_available_in_modal(self):
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.models import CaptureJob, SaveResult
        from face_attendance.storage import SnapshotService
        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            window.show()
            app.processEvents()
            live = window.screens[0]
            try:
                crop = np.full((100, 80, 3), (10, 240, 10), np.uint8)
                context = np.full((240, 320, 3), (240, 10, 10), np.uint8)
                job = CaptureJob('crop-event', 1, 'E1', 'Example', 1700000000, 3, crop,
                                 context_frame=context, face_box=(20, 100, 120, 20),
                                 crop_box=(20, 100, 120, 20), source_sequence=5, source_generation=1)
                snapshot = SnapshotService(Path(directory) / 'captures').save(job)
                live.on_saved(SaveResult(job, 'saved', 1700000001, snapshot))
                row = live.activity._rows[0]
                self.assertEqual(row.avatar._pixmap.width(), 80)
                self.assertGreater(row.avatar._pixmap.toImage().pixelColor(0, 0).green(), 220)
                row.activated.emit(row.entry)
                app.processEvents()
                dialog = live._activity_dialog
                self.assertTrue(dialog.context_button.isVisible())
                self.assertEqual(dialog.capture_photo._pixmap.width(), 80)
                dialog.context_button.click()
                self.assertEqual(dialog.capture_photo._pixmap.width(), 320)
                self.assertGreater(dialog.capture_photo._pixmap.toImage().pixelColor(0, 0).blue(), 220)
                dialog.context_button.click()
                self.assertEqual(dialog.capture_photo._pixmap.width(), 80)
                dialog.accept()
                app.processEvents()
            finally:
                window.close()
                app.processEvents()

    def test_saved_photo_opens_from_row_and_keyboard_and_closes_cleanly(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QGraphicsBlurEffect, QPushButton
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.models import CaptureJob, SaveResult
        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            window.show()
            app.processEvents()
            live = window.screens[0]
            try:
                employee = window.engine.employee_rows()[0]
                enrolled = photo_path(directory, employee['name'])
                cv2.imwrite(str(enrolled), np.full((100, 80, 3), (240, 10, 10), np.uint8))
                saved = Path(directory) / 'event.png'
                cv2.imwrite(str(saved), np.full((100, 80, 3), (10, 240, 10), np.uint8))
                frame = np.full((100, 80, 3), (10, 10, 240), np.uint8)
                job = CaptureJob('specific-event', 1, employee['employee_id'], 'Display name', 1700000000, 3, frame)
                window.engine.attendanceSaved.emit(SaveResult(job, 'saved', 1700000001, str(saved)))
                app.processEvents()
                row = live.activity._rows[0]
                self.assertEqual(row.avatar._pixmap.toImage().pixelColor(0, 0).green(), 240)
                QTest.mouseClick(row, Qt.MouseButton.LeftButton)
                app.processEvents()
                dialog = live._activity_dialog
                self.assertTrue(dialog.isVisible())
                self.assertEqual(dialog.photo_paths, [enrolled])
                self.assertEqual(dialog.capture_photo._pixmap.toImage().pixelColor(0, 0).green(), 240)
                self.assertIsInstance(dialog.backdrop.graphicsEffect(), QGraphicsBlurEffect)
                self.assertIsNone(window.centralWidget().graphicsEffect())
                self.assertFalse(window.engine.paused)
                QTest.keyClick(dialog, Qt.Key.Key_Escape)
                app.processEvents()
                self.assertIsNone(live._activity_dialog)
                self.assertTrue(window.isEnabled())
                QTest.keyClick(row, Qt.Key.Key_Return)
                app.processEvents()
                dialog = live._activity_dialog
                self.assertIsNotNone(dialog)
                next(button for button in dialog.findChildren(QPushButton) if button.text() == 'Done').click()
                app.processEvents()
                self.assertIsNone(live._activity_dialog)
            finally:
                window.close()
                app.processEvents()

    def test_failed_capture_uses_event_frame_and_missing_photos_are_safe(self):
        from PyQt6.QtCore import Qt, QPoint
        from PyQt6.QtTest import QTest
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.models import CaptureJob, SaveResult
        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            window.show()
            app.processEvents()
            live = window.screens[0]
            try:
                frame = np.full((120, 160, 3), (5, 20, 230), np.uint8)
                job = CaptureJob('failed', 1, 'missing-id', 'Example', 1700000000, 3, frame)
                window.engine.attendanceFailed.emit(SaveResult(job, 'error', error='Disk unavailable'))
                frame[:] = 0  # The UI must own its pixels after the event.
                row = live.activity._rows[0]
                self.assertEqual(row.avatar._pixmap.toImage().pixelColor(0, 0).red(), 230)
                QTest.keyClick(row, Qt.Key.Key_Space)
                app.processEvents()
                dialog = live._activity_dialog
                self.assertEqual(dialog.photo_paths, [])
                self.assertIn('No enrollment photo', dialog.photo_count.text())
                self.assertFalse(dialog.next.isEnabled())
                self.assertFalse(dialog.previous.isEnabled())
                QTest.mouseClick(dialog, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))
                app.processEvents()
                self.assertIsNone(live._activity_dialog)
                live._add_activity('No image', 'Save failed', 'No frame', 'missing-id')
                row = live.activity._rows[0]
                self.assertTrue(row.avatar._pixmap.isNull())
                QTest.mouseClick(row, Qt.MouseButton.LeftButton)
                app.processEvents()
                self.assertTrue(live._activity_dialog.capture_photo._pixmap.isNull())
                window.close()  # Closing the parent must remove the modal as well.
                app.processEvents()
                self.assertIsNone(live._activity_dialog)
            finally:
                window.close()
                app.processEvents()

    def test_enrollment_lookup_uses_id_and_rejects_ambiguous_legacy_names(self):
        from face_attendance.dashboard.widgets.activity_detail import enrollment_photos
        with tempfile.TemporaryDirectory() as directory:
            settings = temp_settings(directory)
            rows = [dict(name='A B', employee_id='E1'), dict(name='A_B', employee_id='E2'),
                    dict(name='Alias', employee_id='E1')]
            folder = Path(directory) / 'enrollment_photos'
            folder.mkdir()
            (folder / 'A_B.jpg').write_bytes(b'legacy')
            self.assertEqual(enrollment_photos(settings, 'E1', rows), [])
            canonical = photo_path(directory, 'A B')
            canonical.write_bytes(b'canonical')
            alias = photo_path(directory, 'Alias')
            alias.write_bytes(b'alias')
            self.assertEqual(enrollment_photos(settings, 'E1', rows), [canonical, alias])
            self.assertEqual(enrollment_photos(settings, 'E2', rows), [])
            self.assertEqual(enrollment_photos(settings, 'unknown', rows), [])
