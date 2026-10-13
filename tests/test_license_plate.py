import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import time

import numpy as np

from license_plate.core import CapturePolicy, Plate, read_plates, save_capture


class LicensePlateTests(unittest.TestCase):
    def test_requires_consecutive_readings_and_cooldown(self):
        plate = Plate("ABC123", .95, (0, 0, 20, 10))
        policy = CapturePolicy(2, 30)
        self.assertEqual(policy.eligible([plate], 0), [])
        self.assertEqual(policy.eligible([], 1), [])
        self.assertEqual(policy.eligible([plate], 2), [])
        self.assertEqual(policy.eligible([plate], 3), [plate])
        policy.mark_saved(plate, 3)
        self.assertEqual(policy.eligible([plate], 32), [])
        self.assertEqual(policy.eligible([plate], 33), [plate])

    def test_adapter_filters_missing_low_confidence_and_invalid_boxes(self):
        box = NS(x1=-1, y1=0, x2=20, y2=10)
        results = [NS(ocr=NS(text="abc123", confidence=[.9, 1]),
                      detection=NS(bounding_box=box)), NS(ocr=None),
                   NS(ocr=NS(text="low", confidence=.2), detection=NS(bounding_box=box))]
        reader = NS(predict=lambda frame: results)
        plates = read_plates(reader, np.zeros((10, 20, 3), dtype=np.uint8), .8)
        self.assertEqual(plates, [Plate("ABC123", .95, (0, 0, 20, 10))])

    def test_capture_writes_full_image_crop_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = np.zeros((10, 20, 3), dtype=np.uint8)
            record = save_capture(directory, frame, Plate("../ABC", .9, (0, 0, 10, 5)), 0, True)
            self.assertTrue((Path(directory) / record["image"]).is_file())
            self.assertTrue((Path(directory) / record["crop"]).is_file())
            self.assertIn('"automatic": true', (Path(directory) / "captures.jsonl").read_text())

    def test_failed_image_write_does_not_log_a_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("license_plate.core.cv2.imwrite", return_value=False):
                with self.assertRaises(OSError):
                    save_capture(directory, np.zeros((10, 20, 3), dtype=np.uint8),
                                 Plate("ABC", .9, (0, 0, 10, 5)), 0, False)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_worker_auto_capture_saves_once_and_stops_without_owning_shared_camera(self):
        from face_attendance.config import Config
        from license_plate.screen import PlateWorker
        plate = Plate("ABC123", .95, (0, 0, 10, 5))
        frame = np.zeros((10, 20, 3), dtype=np.uint8)
        sequence = [0]

        def shared():
            sequence[0] += 1
            return NS(sequence=sequence[0], captured_at=time.monotonic(), frame=frame)

        readings = [0]
        with tempfile.TemporaryDirectory() as directory:
            worker = PlateWorker(Config(), directory, .8, 2, 30, 0,
                                 shared=shared, recognizer_factory=lambda: object())
            worker.automatic.set()

            def read(*args):
                readings[0] += 1
                if readings[0] == 4:
                    worker.requestInterruption()
                return [plate]

            with patch("license_plate.screen.read_plates", side_effect=read):
                worker.start()
                finished = worker.wait(3000)
                if not finished:
                    worker.stop()
                self.assertTrue(finished, "worker did not stop after cancellation")
            self.assertIsNone(worker.camera)
            self.assertEqual(len((Path(directory) / "captures.jsonl").read_text().splitlines()), 1)
            self.assertIsNone(worker.latest)

    def test_screen_manual_capture_and_access_revocation(self):
        from PyQt6.QtCore import QObject, pyqtSignal
        from PyQt6.QtWidgets import QApplication
        from face_attendance.settings import Settings
        from license_plate.screen import LicensePlateScreen
        from face_attendance.dashboard.access import AccessControl
        import threading

        class Engine(QObject):
            runningChanged = pyqtSignal(bool)

        application = QApplication.instance() or QApplication([])
        screen = LicensePlateScreen(Engine(), Settings())
        try:
            screen.worker = NS(manual=threading.Event(), automatic=threading.Event(),
                               isRunning=lambda: False, stop=lambda: None)
            screen.capture()
            self.assertTrue(screen.worker.manual.is_set())
            screen.auto.setChecked(True)
            self.assertTrue(screen.worker.automatic.is_set())
            screen.apply_access(AccessControl(NS(permissions=[])))
            self.assertFalse(screen.worker.manual.is_set())
            self.assertFalse(screen.worker.automatic.is_set())
            self.assertFalse(screen.auto.isChecked())
        finally:
            screen.close()
            application.processEvents()
