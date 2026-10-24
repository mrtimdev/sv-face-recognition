"""Monitor pop-out controls and fullscreen keyboard handling share one screen."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMainWindow

from tests.test_dashboard import _app, temp_settings
from tests.test_pipeline import FakeBackend
from face_attendance.dashboard.engine import AttendanceEngine
from face_attendance.dashboard.screens.live import LiveScreen
from face_attendance.dashboard.widgets import VideoView
from license_plate.screen import LicensePlateScreen


class DetachedControlTests(unittest.TestCase):
    def setUp(self):
        self.app = _app()
        self.directory = tempfile.TemporaryDirectory()
        self.settings = temp_settings(self.directory.name)
        self.engine = AttendanceEngine(self.settings, backend=FakeBackend(), use_lock=False)
        self.windows = []
        self.screens = []

    def tearDown(self):
        for screen in self.screens:
            screen.close()
        for window in self.windows:
            window.close()
        self.engine.stop()
        self.app.processEvents()
        self.directory.cleanup()

    def monitor(self, kind):
        if kind == 'live':
            screen = LiveScreen(self.engine, self.settings)
        else:
            with patch('license_plate.screen.ROOT', Path(self.directory.name)):
                screen = LicensePlateScreen(self.engine, self.settings)
        window = QMainWindow()
        window.setCentralWidget(screen)
        window.resize(1200, 900)
        window.show()
        window.activateWindow()
        self.app.processEvents()
        self.screens.append(screen)
        self.windows.append(window)
        return screen, window

    def test_hud_and_more_menu_emit_request_without_starting_workers(self):
        for kind in ('live', 'plate'):
            with self.subTest(kind=kind):
                screen, window = self.monitor(kind)
                calls = []
                screen.detachRequested.connect(lambda: calls.append(True))
                self.assertEqual(screen.preview_hud.detach.accessibleName(), 'Open in new window')
                self.assertEqual(screen.detach_action.text(), 'Open in new window')
                self.assertIn(screen.detach_action, screen.preview_hud.more.menu().actions())
                screen.preview_hud.detach.click()
                screen.detach_action.trigger()
                self.assertEqual(len(calls), 2)
                screen.set_detached(True)
                self.assertEqual(screen.preview_hud.detach.toolTip(), 'Return to dashboard')
                self.assertEqual(screen.preview_hud.detach.accessibleName(), 'Return to dashboard')
                self.assertEqual(screen.detach_action.text(), 'Return to dashboard')
                screen.preview_hud.detach.click()
                self.assertEqual(len(calls), 3)
                screen.set_detached(False)
                self.assertEqual(screen.detach_action.text(), 'Open in new window')
                self.assertFalse(self.engine.running)
                if kind == 'plate':
                    self.assertIsNone(screen.worker)
                window.hide()

    def test_shared_detach_control_remains_visible_at_compact_width(self):
        video = VideoView()
        hud = video.enable_controls()
        video.resize(360, 280)
        video.show()
        self.app.processEvents()
        try:
            self.assertTrue(hud.detach.isVisible())
            origin = hud.detach.mapTo(video, QPoint(0, 0))
            self.assertGreaterEqual(origin.x(), 0)
            self.assertLessEqual(origin.x() + hud.detach.width(), video.width())
            hud.set_detached(True)
            self.assertEqual(hud.detach.accessibleName(), 'Return to dashboard')
        finally:
            video.close()

    def test_escape_from_child_control_exits_each_detached_monitor_fullscreen(self):
        for kind in ('live', 'plate'):
            with self.subTest(kind=kind):
                screen, window = self.monitor(kind)
                screen.set_detached(True)
                screen._toggle_fullscreen()
                self.app.processEvents()
                self.assertTrue(window.isFullScreen())
                button = screen.preview_hud.detach
                button.setFocus()
                self.app.processEvents()
                self.assertTrue(button.hasFocus())
                QTest.keyClick(button, Qt.Key.Key_Escape)
                self.app.processEvents()
                self.assertFalse(screen._fs_active)
                self.assertFalse(window.isFullScreen())
                self.assertEqual(screen.preview_hud.detach.accessibleName(), 'Return to dashboard')
                screen._exit_fullscreen()
                self.assertFalse(window.isFullScreen())
                window.hide()

    def test_live_hide_restores_fullscreen_and_preserves_hidden_panels(self):
        screen, window = self.monitor('live')
        screen.log_card.hide()
        screen._toggle_fullscreen()
        self.assertTrue(screen._fs_active)
        screen.hide()
        self.app.processEvents()
        self.assertFalse(screen._fs_active)
        self.assertFalse(window.isFullScreen())
        self.assertTrue(screen.log_card.isHidden())
        self.assertFalse(screen.escape_shortcut.isEnabled())
