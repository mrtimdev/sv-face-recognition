import tempfile
import unittest
from datetime import date
from pathlib import Path

from license_plate.core import Plate, PlateTracker, load_captures
from license_plate.maintenance import demo_fleet, maintenance_status, plate_key, save_fleet, load_fleet


class MaintenanceTests(unittest.TestCase):
    def test_demo_due_boundaries(self):
        today = date(2026, 10, 17)
        self.assertEqual([maintenance_status(row, today) for row in demo_fleet(today)],
                         ["OVERDUE", "DUE SOON", "CLEAR"])
        self.assertEqual(maintenance_status({"due_date": today.isoformat()}, today), "DUE SOON")

    def test_registry_round_trip_and_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fleet.json"
            rows = demo_fleet()
            save_fleet(path, rows)
            self.assertEqual(load_fleet(path), rows)
            with self.assertRaises(ValueError):
                save_fleet(path, rows + [dict(rows[0], plate="3a 1234")])
            self.assertEqual(load_fleet(path), rows)
        self.assertNotEqual(plate_key("O-123"), plate_key("0-123"))

    def test_track_stability_expiry_and_distinct_plates(self):
        tracker = PlateTracker()
        plate = Plate("3A-1234", .9, (0, 0, 10, 5))
        self.assertEqual(tracker.update([plate], 0)[0].track_id, 1)
        self.assertEqual(tracker.update([Plate("3a1234", .9, plate.box)], 1)[0].track_id, 1)
        self.assertEqual(tracker.update([plate], 12)[0].track_id, 2)

    def test_history_skips_incomplete_or_corrupt_records(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "captures.jsonl").write_text('broken\n{}\n')
            self.assertEqual(load_captures(directory), [])


class MaintenanceScreenTests(unittest.TestCase):
    def test_confirmed_live_alert_and_history_restore(self):
        from unittest.mock import patch
        from PyQt6.QtCore import QObject, pyqtSignal
        from PyQt6.QtWidgets import QApplication
        from face_attendance.settings import Settings
        from license_plate.screen import LicensePlateScreen
        from license_plate.core import save_capture
        import numpy as np

        class Engine(QObject):
            runningChanged = pyqtSignal(bool)

        from tests.test_dashboard import _app
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_fleet(root / "plate_maintenance.json", demo_fleet())
            plate = Plate("3A-1234", .95, (0, 0, 10, 5), 1)
            save_capture(root / "plate_captures", np.zeros((10, 20, 3), dtype=np.uint8), plate, 0, True)
            with patch("license_plate.screen.ROOT", root):
                screen = LicensePlateScreen(Engine(), Settings())
            try:
                self.assertEqual(screen.history.rowCount(), 1)
                self.assertEqual(screen.history.item(0, 4).text(), "#1")
                screen.history.selectRow(0)
                self.assertFalse(screen.crop_preview.pixmap().isNull())
                screen._result([plate])
                self.assertNotIn("OVERDUE", screen.maintenance_message.text())
                screen._result([plate])
                screen._result([plate])
                self.assertIn("OVERDUE", screen.maintenance_message.text())
                self.assertIn("Demo Truck 01", screen.maintenance_message.text())
                screen._result([])
                self.assertIn("Waiting", screen.maintenance_message.text())
            finally:
                screen.close()
                application.processEvents()
