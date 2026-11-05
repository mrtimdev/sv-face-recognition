"""Camera HUD state, contrast and layout regressions; no physical camera."""
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

import numpy as np

from tests.test_dashboard import _app, temp_settings


class PreviewHUDTests(unittest.TestCase):
    def test_activity_uses_recorded_time_and_retains_recent_failures(self):
        from datetime import datetime
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.models import CaptureJob, SaveResult
        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            window.show()
            live = window.screens[0]
            try:
                job = CaptureJob("event", 1, "EMP1", "Example Person", 1000, 3, None)
                window.engine.attendanceSaved.emit(SaveResult(job, "saved", recorded_at=1000))
                row = live.activity._rows[0]
                self.assertEqual(row.time_label.text(), datetime.fromtimestamp(1000).strftime("%H:%M:%S"))
                self.assertEqual(row.badge.text(), "Check-in saved")
                self.assertEqual(live.activity_count.text(), "1 saved")
                self.assertFalse(live.activity.empty_state.isVisible())
                for index in range(10):
                    window.engine.attendanceFailed.emit(SaveResult(job, "error", error=f"Failure {index}"))
                app.processEvents()
                self.assertEqual(live.activity.count(), 8)
                self.assertEqual(live.activity._rows[0].badge.text(), "Save failed")
                self.assertIn("Failure 9", live.activity._rows[0].detail_label.toolTip())
                self.assertEqual(live.activity_count.text(), "1 saved")
                live.activity.clear()
                self.assertTrue(live.activity.empty_state.isVisible())
            finally:
                window.close()
                app.processEvents()

    def test_controls_follow_engine_pause_storage_and_connection(self):
        from face_attendance.dashboard.app import MainWindow
        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            live = window.screens[0]
            hud = live.preview_hud
            try:
                self.assertFalse(hud.pause.isEnabled())
                self.assertFalse(hud.capture.isEnabled())
                with patch.object(type(window.engine), "running", new_callable=PropertyMock,
                                  return_value=True):
                    window.engine.runningChanged.emit(True)
                    window.engine.frameReady.emit(np.zeros((240, 320, 3), np.uint8))
                    window.engine.statsReady.emit({"status": "CONNECTED", "storage_ready": True})
                    self.assertIn("LIVE", hud.status.text())
                    self.assertTrue(hud.capture.isEnabled())
                    self.assertEqual(hud.record.text(), "Auto Record")
                    hud.pause.click()
                    self.assertTrue(window.engine.paused)
                    self.assertEqual(hud.pause.text(), "Resume")
                    self.assertIn("PAUSED", hud.status.text())
                    self.assertTrue(live.video.has_frame())
                    # External pause changes use the same state synchronization.
                    window.engine.set_paused(False)
                    self.assertEqual(hud.pause.text(), "Pause")
                    window.engine.statsReady.emit({"status": "CONNECTED", "storage_ready": False})
                    self.assertEqual(hud.record.text(), "Standby")
                    window.engine.statsReady.emit({"status": "CAMERA DISCONNECTED"})
                    self.assertIn("OFFLINE", hud.status.text())
                    self.assertFalse(hud.capture.isEnabled())
                    with patch.object(window.engine, "restart") as restart:
                        hud.restart.click()
                        restart.assert_called_once()
                window.engine.runningChanged.emit(False)
                self.assertIn("STOPPED", hud.status.text())
                self.assertFalse(live.video.has_frame())
            finally:
                window.close()
                app.processEvents()

    def test_contrast_adapts_without_changing_camera_pixels(self):
        from face_attendance.dashboard.widgets.video_view import VideoView
        app = _app()
        video = VideoView()
        hud = video.enable_controls()
        video.resize(640, 360)
        video.show()
        app.processEvents()
        try:
            dark = np.zeros((360, 640, 3), np.uint8)
            bright = np.full_like(dark, 255)
            with patch("face_attendance.dashboard.widgets.preview_hud.monotonic", return_value=1):
                video.set_frame(dark)
            self.assertTrue(hud.pause._light_ink)
            with patch("face_attendance.dashboard.widgets.preview_hud.monotonic", return_value=2):
                video.set_frame(bright)
            self.assertFalse(hud.pause._light_ink)
            self.assertTrue(np.all(dark == 0))
            self.assertTrue(np.all(bright == 255))
            with patch("face_attendance.dashboard.widgets.preview_hud.monotonic", return_value=2.1):
                video.set_frame(dark)
            self.assertFalse(hud.pause._light_ink)  # throttled to avoid flicker
            hud.reset_contrast()
            mixed = bright.copy()
            mixed[:, 320:] = 0
            with patch("face_attendance.dashboard.widgets.preview_hud.monotonic", return_value=3):
                video.set_frame(mixed)
            self.assertFalse(hud.record._light_ink)
            self.assertTrue(hud.more._light_ink)
            video.show_message("Stopped")
            self.assertTrue(hud.record._light_ink)
            # Rendering the HUD alone must leave open areas fully transparent.
            from PyQt6.QtCore import Qt
            from PyQt6.QtGui import QPixmap
            pixmap = QPixmap(hud.size())
            pixmap.fill(Qt.GlobalColor.transparent)
            hud.render(pixmap)
            rendered = pixmap.toImage()
            self.assertEqual(rendered.pixelColor(320, 180).alpha(), 0)
            self.assertEqual(rendered.pixelColor(hud.dock.x() + 2, hud.dock.y() + 2).alpha(), 0)
        finally:
            video.close()

    def test_compact_layout_and_fullscreen_share_controls(self):
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.dashboard.widgets.video_view import VideoView
        app = _app()
        video = VideoView()
        hud = video.enable_controls()
        video.resize(320, 360)
        video.show()
        app.processEvents()
        try:
            self.assertFalse(hud.metrics.isVisible())
            self.assertTrue(hud.rect().contains(hud.top.geometry()))
            self.assertTrue(hud.rect().contains(hud.dock.geometry()))
            self.assertFalse(hud.top.geometry().intersects(hud.dock.geometry()))
            buttons = (hud.record, hud.capture, hud.pause, hud.restart, hud.more)
            for button in buttons:
                self.assertTrue(hud.dock.rect().contains(button.geometry()))
                self.assertTrue(button.accessibleName())
        finally:
            video.close()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            window.show()
            app.processEvents()
            live = window.screens[0]
            original_hud = live.preview_hud
            try:
                live.preview_hud.fullscreen.click()
                app.processEvents()
                self.assertTrue(window.isFullScreen())
                self.assertIs(live.preview_hud, original_hud)
                self.assertTrue(live.preview_hud.more.isVisible())
                live.preview_hud.fullscreen.click()
                app.processEvents()
                self.assertFalse(window.isFullScreen())
                self.assertTrue(window.sidebar.isVisible())
                self.assertTrue(live.requirements_card.isVisible())
                live.fullscreen_action.trigger()
                app.processEvents()
                live.preview_hud.settings.click()
                app.processEvents()
                self.assertFalse(window.isFullScreen())
                self.assertTrue(window.sidebar.isVisible())
                self.assertNotEqual(window.stack.currentIndex(), 0)
            finally:
                window.close()
                app.processEvents()
