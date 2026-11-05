"""Monitor layout, full-screen restoration and preview work limits."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import numpy as np
from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMainWindow, QLabel

from tests.test_dashboard import _app
from face_attendance.settings import Settings
from face_attendance.dashboard.access import AccessControl
from license_plate.screen import LicensePlateScreen


class Engine(QObject):
    runningChanged = pyqtSignal(bool)


class PlateMonitorTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.directory = tempfile.TemporaryDirectory()
        self.window = QMainWindow()
        self.window.sidebar = QLabel("Navigation", self.window)
        self.window.header = QLabel("Header", self.window)
        with patch('license_plate.screen.ROOT', Path(self.directory.name)):
            self.screen = LicensePlateScreen(Engine(), Settings())
        self.window.setCentralWidget(self.screen)
        self.window.resize(1360, 950)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.screen.worker = None
        self.screen.close()
        self.window.close()
        self.app.processEvents()
        self.directory.cleanup()

    def test_fullscreen_escape_restores_panels_and_preserves_hidden_settings(self):
        screen = self.screen
        original_state = self.window.windowState()
        screen.preview_hud.fullscreen.click()
        self.app.processEvents()
        self.assertTrue(screen._fs_active)
        self.assertTrue(self.window.isFullScreen())
        self.assertTrue(screen.details_card.isHidden())
        self.assertTrue(self.window.sidebar.isHidden())
        self.assertTrue(screen.preview.isVisible())
        self.assertEqual(screen.preview_hud.fullscreen.accessibleName(), 'Exit full screen')
        QTest.keyClick(self.window, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertFalse(screen._fs_active)
        self.assertEqual(self.window.windowState(), original_state)
        self.assertFalse(screen.details_card.isHidden())
        self.assertFalse(self.window.sidebar.isHidden())
        self.assertTrue(screen.setup.isHidden())
        self.assertEqual(screen.preview.minimumHeight(), 520)

    def test_fullscreen_navigation_and_settings_restore_window(self):
        screen = self.screen
        screen.settings_button.setChecked(True)
        screen._toggle_fullscreen()
        self.assertTrue(screen.setup.isHidden())
        screen._open_settings()
        self.assertFalse(screen._fs_active)
        self.assertFalse(screen.setup.isHidden())
        screen._toggle_fullscreen()
        screen.hide()
        self.assertFalse(screen._fs_active)
        self.assertFalse(self.window.isFullScreen())

    def test_preview_skips_duplicate_and_hidden_frames_and_controls_honor_access(self):
        screen = self.screen
        packet = NS(sequence=1, generation=1, frame=np.zeros((480,640,3), np.uint8))
        screen.worker = NS(preview_packet=lambda: packet, isRunning=lambda: True,
                           inference_ms=32, automatic=threading.Event(), manual=threading.Event(),
                           stop=lambda: None)
        with patch.object(screen.preview, 'set_frame', wraps=screen.preview.set_frame) as display:
            screen._preview()
            screen._preview()
            self.assertEqual(display.call_count, 1)
            self.assertTrue(screen.preview_hud.capture.isEnabled())
            packet.sequence += 1
            screen._preview()
            self.assertEqual(display.call_count, 2)
            screen.hide()
            packet.sequence += 1
            screen._preview()
            self.assertEqual(display.call_count, 2)
        screen.apply_access(AccessControl(NS(permissions=['view_dashboard','control_engine'])))
        self.assertFalse(screen.preview_hud.capture.isEnabled())
        self.assertFalse(screen.preview_hud.pause.isEnabled())

    def test_restricted_system_statistics_do_not_crash_dashboard(self):
        from face_attendance.dashboard.sysmon import _take_snapshot, static_info
        with patch('face_attendance.dashboard.sysmon.psutil.swap_memory', side_effect=OSError), \
                patch('face_attendance.dashboard.sysmon.psutil.boot_time', side_effect=PermissionError), \
                patch('subprocess.run', return_value=NS(stdout='')):
            snap, _, _ = _take_snapshot(None, None, 0)
            self.assertEqual(static_info().swap_total_bytes, 0)
        self.assertEqual(snap.swap_total_bytes, 0)
        self.assertEqual(snap.uptime_sec, 0)

    def test_history_decodes_only_selected_crop(self):
        records = [dict(text=f'PLATE{i}', timestamp='2026-10-18T00:00:00+00:00',
                        confidence=.95, image=f'{i}.jpg', crop=f'{i}-crop.jpg') for i in range(200)]
        with patch('license_plate.screen.load_captures', return_value=records), \
                patch('cv2.imread', return_value=None) as read:
            self.screen._load_history()
        self.assertEqual(self.screen.history.rowCount(), 200)
        self.assertEqual(read.call_count, 1)

    def test_dashboard_navigation_restores_chrome_and_close_exits_fullscreen(self):
        from face_attendance.dashboard.app import MainWindow, NAV
        from tests.test_dashboard import temp_settings
        window = MainWindow(temp_settings(self.directory.name), use_lock=False)
        window.show()
        try:
            index = next(i for i, (_, title) in enumerate(NAV) if title == 'License Plates')
            window.nav.setCurrentRow(index)
            self.app.processEvents()
            screen = window.screens[index]
            screen.preview_hud.fullscreen.click()
            self.app.processEvents()
            self.assertTrue(window.isFullScreen())
            self.assertFalse(window.sidebar.isVisible())
            window.nav.setCurrentRow(0)
            self.app.processEvents()
            self.assertFalse(window.isFullScreen())
            self.assertTrue(window.sidebar.isVisible())
            window.nav.setCurrentRow(index)
            screen.preview_hud.fullscreen.click()
            window.close()
            self.app.processEvents()
            self.assertFalse(window.isVisible())
            self.assertFalse(screen._fs_active)
        finally:
            window.close()
            self.app.processEvents()

    def test_restart_waits_for_busy_worker_without_blocking_ui(self):
        from face_attendance.config import Config
        from license_plate.screen import PlateWorker
        entered, release = threading.Event(), threading.Event()
        packet = NS(sequence=1, generation=1, captured_at=time.monotonic(),
                    frame=np.zeros((10,20,3), np.uint8))
        worker = PlateWorker(Config(), self.directory.name, .8, 1, 30, 0,
                             shared=lambda: packet, recognizer_factory=object)
        self.screen.worker = worker
        worker.finished.connect(self.screen._finished)

        def read(*args):
            entered.set()
            release.wait(3)
            return []

        with patch('license_plate.screen.read_plates', side_effect=read), \
                patch('license_plate.screen.guard', return_value=True), \
                patch.object(self.screen, 'start') as start:
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                before = time.monotonic()
                self.screen._restart()
                self.assertLess(time.monotonic()-before, .6)
                self.assertTrue(self.screen._restart_pending)
                start.assert_not_called()
            finally:
                release.set()
                worker.wait(3000)
                self.app.processEvents()
                QTest.qWait(5)
            start.assert_called_once()
