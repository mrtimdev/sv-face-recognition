import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np

from face_attendance.models import CaptureJob
from face_attendance.repository import AttendanceRepository
from face_attendance.storage import SnapshotService, delete_capture, evidence_files


class EvidenceDeletionTests(unittest.TestCase):
    def test_removes_only_selected_capture_files_preserves_records_and_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = SnapshotService(root / 'captures')
            frame = np.full((80, 80, 3), 127, np.uint8)
            job = CaptureJob(str(uuid4()), 1, 'E1', 'Alice', 1000, 3, frame, context_frame=frame)
            repo = AttendanceRepository(root / 'attendance.db', clock=lambda: 1000)
            try:
                saved = repo.record(job, snapshots)
                self.assertEqual(saved.outcome, 'saved')
                unrelated = snapshots.directory / 'other.jpg'
                unrelated.write_bytes(b'keep')
                enrollment = root / 'enrollment_photos' / 'Alice.jpg'
                enrollment.parent.mkdir()
                enrollment.write_bytes(b'enrollment')
                self.assertEqual(delete_capture(saved.snapshot, snapshots.directory), 3)
                self.assertTrue(all(not path.exists() for path in evidence_files(saved.snapshot)))
                self.assertEqual(repo.connection.execute('SELECT COUNT(*) FROM attendance').fetchone()[0], 1)
                self.assertEqual(len(repo.pending_outbox()), 1)
                self.assertEqual(enrollment.read_bytes(), b'enrollment')
                self.assertEqual(unrelated.read_bytes(), b'keep')
                self.assertEqual(delete_capture(saved.snapshot, snapshots.directory), 0)
            finally:
                repo.close()

    def test_rejects_external_paths_and_linked_context_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            captures, outside = root / 'captures', root / 'outside'
            captures.mkdir()
            outside.mkdir()
            image = outside / 'evidence.jpg'
            image.write_bytes(b'keep')
            with self.assertRaises(ValueError):
                delete_capture(image, captures)
            local = captures / image.name
            local.write_bytes(b'keep')
            (captures / 'context').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                delete_capture(local, captures)
            self.assertTrue(local.exists())
            self.assertEqual(image.read_bytes(), b'keep')

    def test_failure_keeps_thumbnail_available_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = SnapshotService(root)
            frame = np.full((80, 80, 3), 127, np.uint8)
            path = snapshots.save(CaptureJob(str(uuid4()), 1, 'E1', 'Alice', 1, 3, frame, context_frame=frame))
            original = Path.unlink
            def fail_metadata(file, *args, **kwargs):
                if file.suffix == '.json':
                    raise PermissionError('locked metadata')
                return original(file, *args, **kwargs)
            with patch.object(Path, 'unlink', fail_metadata), self.assertRaises(PermissionError):
                delete_capture(path, root)
            self.assertTrue(Path(path).exists())
            self.assertEqual(delete_capture(path, root), 2)


class EmployeeEvidenceUITests(unittest.TestCase):
    def test_delete_button_cancellation_then_confirmation_refreshes_gallery(self):
        from PyQt6.QtWidgets import QMessageBox, QPushButton
        from face_attendance.dashboard.app import MainWindow
        from tests.test_dashboard import _app, temp_settings
        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            settings = temp_settings(directory)
            snapshots = SnapshotService(settings.capture_dir)
            frame = np.full((80, 80, 3), 127, np.uint8)
            path = snapshots.save(CaptureJob(str(uuid4()), 1, 'E1', 'Seed', 1, 3, frame, context_frame=frame))
            window = MainWindow(settings, use_lock=False)
            window.show()
            screen = window.screens[2]
            try:
                screen.employee_table.selectRow(0)
                screen._show_profile()
                app.processEvents()
                button = next(b for b in screen.gallery_container.findChildren(QPushButton)
                              if b.property('evidenceDelete'))
                self.assertIn('Delete evidence capture', button.accessibleName())
                with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
                    button.click()
                self.assertTrue(Path(path).exists())
                with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
                    button.click()
                self.assertFalse(Path(path).exists())
                self.assertEqual(screen.gallery_count_label.text(), 'none')
                self.assertEqual(screen.rows[0]['samples'], 1)
                screen._open_enrollment(screen.rows[0])
                for index in (0, 1):
                    screen._switch_segment(index)
                    app.processEvents()
                    preview = screen.capture_video if index == 0 else screen.drop
                    self.assertGreaterEqual(preview.height(), 380)
                screen.editor.resize(650, 560)
                app.processEvents()
                self.assertTrue(screen.save_button.isVisible())
                self.assertLessEqual(screen.save_button.mapTo(screen.editor, screen.save_button.rect().bottomRight()).y(),
                                     screen.editor.height())
            finally:
                window.close()
                app.processEvents()
