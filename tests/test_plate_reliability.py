"""Bounded session counters and credential-safe plate capture metadata."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np

from license_plate.core import Plate, save_capture


class PlateReliabilityTests(unittest.TestCase):
    def test_new_evidence_and_existing_metadata_are_private_in_shared_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o755)
            metadata = root / 'captures.jsonl'
            metadata.touch(mode=0o644)
            record = save_capture(root, np.zeros((10, 20, 3), np.uint8),
                                  Plate('ABC', .95, (0, 0, 10, 5)), 0, True)
            for path in (metadata, root / record['image'], root / record['crop']):
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)

    def test_history_icon_retains_only_small_thumbnail(self):
        import cv2
        from PyQt6.QtCore import QObject, pyqtSignal
        from face_attendance.settings import Settings
        from license_plate.screen import LicensePlateScreen
        from tests.test_dashboard import _app

        class Engine(QObject):
            runningChanged = pyqtSignal(bool)

        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cv2.imwrite(str(root / 'crop.jpg'), np.zeros((600, 1600, 3), np.uint8))
            with patch('license_plate.screen.ROOT', root):
                screen = LicensePlateScreen(Engine(), Settings())
            try:
                screen._saved(dict(text='ABC', timestamp='2026-01-01T12:00:00+00:00',
                                   confidence=.95, image='full.jpg', crop='crop.jpg'),
                              directory=root)
                sizes = screen.history.item(0, 3).icon().availableSizes()
                self.assertTrue(sizes)
                self.assertTrue(all(size.width() <= 96 and size.height() <= 48 for size in sizes))
            finally:
                screen.close()
                app.processEvents()

    def test_low_disk_does_not_create_images_or_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('license_plate.core.shutil.disk_usage', return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(OSError, 'Disk space low'):
                    save_capture(directory, np.zeros((10, 20, 3), np.uint8),
                                 Plate('ABC', .95, (0, 0, 10, 5)), 0, True)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_capture_metadata_masks_camera_password(self):
        with tempfile.TemporaryDirectory() as directory:
            record = save_capture(directory, np.zeros((10, 20, 3), np.uint8),
                                  Plate('ABC', .95, (0, 0, 10, 5)),
                                  'rtsp://operator:secret-camera-password@camera.local/stream', True)
            self.assertNotIn('secret-camera-password', record['source'])
            self.assertNotIn('secret-camera-password', (Path(directory) / 'captures.jsonl').read_text())
            self.assertIn('camera.local', record['source'])

    def test_unique_session_cache_is_bounded_and_reports_lower_bound(self):
        from PyQt6.QtCore import QObject, pyqtSignal
        from PyQt6.QtWidgets import QLabel
        from face_attendance.settings import Settings
        from license_plate.screen import LicensePlateScreen
        from tests.test_dashboard import _app

        class Engine(QObject):
            runningChanged = pyqtSignal(bool)

        application = _app()
        screen = LicensePlateScreen(Engine(), Settings())
        try:
            screen._unique_plates.clear()
            screen._capture_count = 0
            screen._unique_plates.update(str(index) for index in range(10_000))
            with tempfile.TemporaryDirectory() as directory:
                screen._saved(dict(text='NEW', timestamp='2026-01-01T12:00:00+00:00',
                                   confidence=.95, image='x.jpg', crop='y.jpg'),
                              directory=Path(directory), load_image=False)
            self.assertEqual(len(screen._unique_plates), 10_000)
            self.assertTrue(screen._unique_plate_overflow)
            self.assertEqual(screen._capture_count, 1)
            self.assertIn('10,000+', [label.text() for label in screen.cards['unique'].findChildren(
                QLabel)])
        finally:
            screen.close()
            application.processEvents()
