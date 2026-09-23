"""Dashboard smoke tests: bridge conversion, window build and a headless engine.

Qt runs on the offscreen platform, so these run anywhere CI can install PyQt6.
The engine is exercised with a fake backend and a fake capture object - the
same injection points the terminal tests use - so no camera is opened.
"""
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from tests.test_pipeline import FakeBackend


_app_instance = None


def _app():
    """One QApplication per test process; it must outlive every test."""
    global _app_instance
    if _app_instance is None:
        from PyQt6.QtWidgets import QApplication
        _app_instance = QApplication([])
    return _app_instance


def temp_settings(root):
    """Settings pointing at a temp dir, with a minimal seeded enrollment catalog."""
    from face_attendance.enrollment import write_encodings
    root = Path(root)
    if not (root / "faces.pickle").exists():
        (root / "employees.json").write_text("{}", encoding="utf-8")
        write_encodings(root / "faces.pickle", {"Seed": [np.ones(128)]})
    from face_attendance.settings import Settings
    return replace(Settings(),
                   db_path=str(root / "attendance.db"),
                   capture_dir=str(root / "captures"),
                   encodings_path=str(root / "faces.pickle"),
                   employees_path=str(root / "employees.json"),
                   log_path=str(root / "observations.csv"),
                   alert_path=str(root / "missing.wav"))


class BridgeTests(unittest.TestCase):
    def test_frame_becomes_pixmap_of_expected_size(self):
        from PyQt6.QtWidgets import QApplication
        from face_attendance.dashboard.bridge import to_pixmap
        _app()
        for height, width in ((480, 640), (720, 1280), (1080, 1920)):
            pixmap = to_pixmap(np.zeros((height, width, 3), dtype=np.uint8))
            self.assertEqual(pixmap.width(), width)
            self.assertEqual(pixmap.height(), height)

    def test_empty_frame_yields_empty_pixmap(self):
        from face_attendance.dashboard.bridge import to_pixmap
        _app()
        self.assertTrue(to_pixmap(None).isNull())


class WindowTests(unittest.TestCase):
    def test_all_five_screens_build_and_switch(self):
        from PyQt6.QtCore import QTimer
        from face_attendance.dashboard.app import MainWindow
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            window.show()
            try:
                self.assertEqual(window.nav.count(), 5)
                for row in range(window.nav.count()):
                    window.nav.setCurrentRow(row)
                    application.processEvents()
                    self.assertEqual(window.stack.currentIndex(), row)
            finally:
                window.close()
                application.processEvents()

    def test_run_dashboard_launches_and_quits(self):
        from PyQt6.QtCore import QTimer
        from face_attendance.dashboard.app import run_dashboard
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            QTimer.singleShot(50, application.quit)  # unblock the event loop
            result = run_dashboard(settings_path)    # loads settings, shows the window
            self.assertEqual(result, 0)
            application.processEvents()

    def test_engine_signals_update_the_window(self):
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.models import CaptureJob, SaveResult
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            try:
                window.engine.statsReady.emit({"status": "CONNECTED"})
                job = CaptureJob("evt-test", 1, "local-1", "Seed", time.time(), 1.0, None)
                window.engine.attendanceSaved.emit(SaveResult(job, "saved", time.time()))
                application.processEvents()
            finally:
                window.close()
                application.processEvents()


class EngineTests(unittest.TestCase):
    class Capture:
        def isOpened(self):
            return True

        def read(self):
            threading.Event().wait(1 / 120)
            return True, np.zeros((240, 320, 3), np.uint8)

        def release(self):
            pass

    def test_start_publishes_frames_then_shutdown_joins_every_worker(self):
        from face_attendance.dashboard.engine import AttendanceEngine
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            engine = AttendanceEngine(temp_settings(directory), backend=FakeBackend(),
                                      capture_factory=lambda: self.Capture(), use_lock=False)
            seen = {"frame": False, "connected": False}
            engine.frameReady.connect(lambda frame: seen.__setitem__("frame", frame.size > 0))
            engine.statsReady.connect(
                lambda stats: seen.__setitem__("connected", stats.get("status") == "CONNECTED"))
            try:
                engine.start()
                self.assertTrue(engine.running)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not all(seen.values()):
                    application.processEvents()
                    time.sleep(0.02)
                self.assertTrue(seen["frame"], "no frame reached the dashboard")
                self.assertTrue(seen["connected"], "camera never reported CONNECTED")
                engine.set_paused(True)
                self.assertTrue(engine.paused)
            finally:
                engine.shutdown()
                application.processEvents()
            self.assertFalse(engine.running)
            for worker in (engine.camera, engine.recognition, engine.persistence):
                self.assertFalse(worker.thread.is_alive())

    def test_second_engine_on_the_same_lock_is_refused(self):
        from face_attendance.dashboard.lock import EngineLock
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attendance.lock"
            first = EngineLock(path)
            second = EngineLock(path)
            try:
                self.assertTrue(first.acquire())
                self.assertTrue(first.held)
                try:
                    self.assertFalse(second.acquire())  # platforms without fcntl
                except RuntimeError:
                    pass  # contended: refused loudly, exactly like the dashboard
            finally:
                first.release()
            self.assertTrue(second.acquire())
            second.release()


class StubEngine:
    """Just the surface EmployeesScreen touches; no threads, no camera."""

    def __init__(self, frame=None):
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Signals(QObject):
            frameReady = pyqtSignal(object)
            catalogChanged = pyqtSignal(object)
            attendanceSaved = pyqtSignal(object)

        self._signals = _Signals()
        self.frameReady = self._signals.frameReady
        self.catalogChanged = self._signals.catalogChanged
        self.attendanceSaved = self._signals.attendanceSaved
        self.running = True
        self._frame = frame
        self.started = 0
        self.stopped = 0

    def grab_clean_frame(self):
        return None if self._frame is None else self._frame.copy()

    def start(self):
        self.started += 1
        self.running = True

    def stop(self):
        self.stopped += 1
        self.running = False

    def reload_catalog(self):
        pass


class PreviewCapture:
    """Fake cv2.VideoCapture for the enrollment preview camera."""

    def __init__(self, frame):
        self._frame = frame
        self.released = False

    def isOpened(self):
        return not self.released

    def read(self):
        time.sleep(0.005)
        if self.released:
            return False, None
        return True, None if self._frame is None else self._frame.copy()

    def release(self):
        self.released = True


class EmployeesScreenTests(unittest.TestCase):
    """The capture wizard must never block the UI thread or get stuck.

    Defects that shipped here over time: the countdown timer was never started
    (the button stayed on 'Capturing in 2...' forever), sample validation ran
    HOG detection synchronously on the UI thread (multi-second freezes), and
    the Live-capture preview reused the attendance engine's camera.
    """

    def _screen(self, directory, frame=None):
        from face_attendance.dashboard.screens.employees import EmployeesScreen
        settings = temp_settings(directory)
        engine = StubEngine(frame=frame)
        screen = EmployeesScreen(engine, settings,
                                 preview_capture_factory=lambda: PreviewCapture(frame))
        screen.quality_check.setChecked(False)
        screen.service.backend = FakeBackend()
        return screen

    def _start_preview(self, application, screen):
        """Real code path: the dedicated preview camera must deliver a frame."""
        screen._toggle_preview()
        self.assertTrue(screen.preview_on, "preview did not start")
        self.assertTrue(self._wait_for(application, lambda: screen._latest_preview_frame()
                                       is not None),
                        "the dedicated preview camera never produced a frame")

    def _wait_for(self, application, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            application.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return condition()

    def test_directory_shows_saved_photo_and_filters_status(self):
        from face_attendance.enrollment import write_encodings
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory)
            try:
                write_encodings(screen.settings.encodings_path, {"Seed": [np.ones(128)], "Waiting": []})
                screen._save_enrollment_photo("Seed", [np.full((120, 120, 3), (30, 100, 220), np.uint8)])
                screen.reload()
                self.assertEqual(screen.employee_table.rowCount(), 2)
                self.assertEqual(screen.employee_table.item(0, 0).toolTip(), "Saved enrollment photo")
                self.assertFalse(screen.employee_table.item(0, 0).icon().isNull())
                screen.status_filter.setCurrentIndex(2)
                self.assertTrue(screen.employee_table.isRowHidden(0))
                self.assertFalse(screen.employee_table.isRowHidden(1))
                screen.search_edit.setText("missing")
                self.assertTrue(screen.employee_table.isRowHidden(1))
                self.assertFalse(screen.empty_label.isHidden())
            finally:
                screen.close()

    def test_edit_and_photo_update_keep_original_label_and_employee_id(self):
        from unittest.mock import patch
        from face_attendance.enrollment import read_encodings
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory)
            try:
                original_id = screen.rows[0]["employee_id"]
                screen.employee_table.selectRow(0)
                with patch('face_attendance.dashboard.screens.employees.QInputDialog.getText',
                           return_value=("Edited employee", True)):
                    screen._edit_selected()
                screen.employee_table.selectRow(0)
                screen._use_selected()
                self.assertEqual(screen.name_edit.text(), "Edited employee")
                self.assertTrue(screen.name_edit.isReadOnly())
                screen.pending = [np.full((240, 320, 3), 128, np.uint8)]
                screen._save()
                self.assertTrue(self._wait_for(application, lambda: not screen.saving))
                self.assertEqual(list(read_encodings(screen.settings.encodings_path)), ["Seed"])
                self.assertEqual(screen.rows[0]["employee_id"], original_id)
                self.assertEqual(screen.rows[0]["employee_name"], "Edited employee")
                self.assertEqual(screen.rows[0]["samples"], 2)
                self.assertTrue(screen._enrollment_photo_path("Seed").exists())
                self.assertFalse(screen.editor.isVisible())
                screen._switch_to_wizard()
                self.assertEqual(screen.name_edit.text(), "")
                self.assertEqual(screen.id_edit.text(), "")
                self.assertFalse(screen.name_edit.isReadOnly())
            finally:
                screen.close()

    def test_delete_cancellation_and_confirmation_preserve_attendance_history(self):
        from unittest.mock import patch
        from PyQt6.QtWidgets import QMessageBox
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory)
            try:
                history = Path(screen.settings.db_path)
                history.write_bytes(b'attendance history sentinel')
                screen._save_enrollment_photo("Seed", [np.ones((100, 100, 3), np.uint8)])
                photo = screen._enrollment_photo_path("Seed")
                screen.employee_table.selectRow(0)
                with patch('face_attendance.dashboard.screens.employees.QMessageBox.question',
                           return_value=QMessageBox.StandardButton.No):
                    screen._remove_employee()
                self.assertEqual(len(screen.rows), 1)
                self.assertTrue(photo.exists())
                with patch('face_attendance.dashboard.screens.employees.QMessageBox.question',
                           return_value=QMessageBox.StandardButton.Yes):
                    screen._remove_employee()
                self.assertEqual(screen.rows, [])
                self.assertFalse(photo.exists())
                self.assertEqual(history.read_bytes(), b'attendance history sentinel')
            finally:
                screen.close()

    def test_photo_filenames_do_not_mix_colliding_employee_labels(self):
        from face_attendance.enrollment import write_encodings
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory)
            try:
                write_encodings(screen.settings.encodings_path, {"A B": [], "A_B": []})
                screen.reload()
                first = screen._enrollment_photo_path("A B")
                second = screen._enrollment_photo_path("A_B")
                self.assertNotEqual(first, second)
                screen._save_enrollment_photo("A B", [np.ones((100, 100, 3), np.uint8)])
                self.assertEqual(screen._find_enrollment_photo("A B", "A B"), first)
                self.assertIsNone(screen._find_enrollment_photo("A_B", "A_B"))
            finally:
                screen.close()

    def test_manual_capture_completes_off_thread(self):
        from PyQt6.QtWidgets import QApplication
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory, np.full((240, 320, 3), 128, np.uint8))
            try:
                self._start_preview(application, screen)
                screen.countdown_box.setValue(0)
                screen._start_countdown()
                self.assertTrue(self._wait_for(application, lambda: len(screen.pending) == 1),
                                "manual capture never produced a sample")
                self.assertEqual(screen.capture_button.text(), "Capture sample")
                self.assertTrue(screen.capture_button.isEnabled())
                self.assertFalse(screen.validating)
            finally:
                screen.close()

    def test_countdown_timer_runs_and_captures(self):
        from PyQt6.QtWidgets import QApplication
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory, np.full((240, 320, 3), 128, np.uint8))
            try:
                self._start_preview(application, screen)
                screen.countdown_box.setValue(2)
                screen._start_countdown()
                self.assertTrue(screen.countdown_timer.isActive(),
                                "countdown must tick: it used to stall forever")
                self.assertTrue(self._wait_for(application, lambda: len(screen.pending) == 1),
                                "countdown never finished the capture")
                self.assertFalse(screen.countdown_timer.isActive())
            finally:
                screen.close()

    def test_probe_auto_captures_with_cooldown(self):
        from PyQt6.QtWidgets import QApplication
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory, np.full((240, 320, 3), 128, np.uint8))
            try:
                self._start_preview(application, screen)
                screen.auto_check.setChecked(True)
                screen._probe_now()
                self.assertTrue(self._wait_for(application, lambda: len(screen.pending) == 1),
                                "auto-capture never added a sample")
                screen._probe_now()
                application.processEvents()
                self.assertEqual(len(screen.pending), 1,
                                 "cooldown must stop burst captures from one visit")
            finally:
                screen.close()

    def test_preview_runs_without_the_attendance_engine(self):
        """Enrollment must not depend on the engine: it owns its camera now."""
        from PyQt6.QtWidgets import QApplication
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory, np.full((240, 320, 3), 128, np.uint8))
            try:
                screen.engine.running = False
                self._start_preview(application, screen)
                self.assertEqual(screen.engine.stopped, 0,
                                 "a stopped engine must not be stopped again")
                screen.countdown_box.setValue(0)
                screen._start_countdown()
                self.assertTrue(self._wait_for(application, lambda: len(screen.pending) == 1),
                                "capture never produced a sample with the engine stopped")
            finally:
                screen.close()

    def test_starting_preview_pauses_and_closing_resumes_attendance(self):
        """One webcam, two consumers: enrollment pauses the engine, then resumes it."""
        from PyQt6.QtWidgets import QApplication
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory, np.full((240, 320, 3), 128, np.uint8))
            try:
                self._start_preview(application, screen)
                self.assertEqual(screen.engine.stopped, 1)
                self.assertFalse(screen.engine.running)
                screen._toggle_preview()
                self.assertFalse(screen.preview_on)
                self.assertEqual(screen.engine.started, 1)
                self.assertTrue(screen.engine.running)
            finally:
                screen.close()
            self.assertEqual(screen.engine.started, 1,
                             "window close must not restart the engine")

    def test_upload_validates_off_thread_and_clears_for_the_next_photo(self):
        """Adding a photo must not freeze the UI, and the next photo must be pickable."""
        from PyQt6.QtWidgets import QApplication
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            screen = self._screen(directory)
            try:
                screen.upload = np.full((240, 320, 3), 128, np.uint8)
                screen._add_upload_sample()
                self.assertEqual(screen.add_upload_button.text(), "Checking photo...")
                self.assertTrue(self._wait_for(application, lambda: len(screen.pending) == 1),
                                "upload validation never finished")
                self.assertEqual(screen.add_upload_button.text(), "Add photo as sample")
                self.assertFalse(screen.add_upload_button.isEnabled())
                self.assertIsNone(screen.upload,
                                  "the added photo must be cleared so another can be chosen")
            finally:
                screen.close()


if __name__ == "__main__":
    unittest.main()
