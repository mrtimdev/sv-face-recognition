"""Quit, relaunch and sign-out stop the dashboard behind a progress card.

The engine runs for real (fake camera and backend) so the worker-thread
shutdown is exercised end to end; spawning the replacement process is the
only thing patched out.
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests import test_dashboard
from tests.test_dashboard import _app, temp_settings
from tests.test_dashboard_access import wait_until
from tests.test_pipeline import FakeBackend


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        _app()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = temp_settings(self._dir.name)

    def _window(self, running=True, use_lock=False):
        from face_attendance.dashboard.app import MainWindow
        window = MainWindow(self.settings, backend=FakeBackend(),
                            capture_factory=lambda: test_dashboard.EngineTests.Capture(),
                            use_lock=use_lock)
        window.show()
        if running:
            window.engine.start()
            self.assertTrue(window.engine.running)

        def cleanup():
            if window.isVisible():
                window._exit_done = True
                window.close()
            _app().processEvents()
        self.addCleanup(cleanup)
        return window

    def _states(self, overlay):
        return [step.state for step in overlay.steps]

    def test_quit_stops_everything_behind_the_progress_card(self):
        from face_attendance.dashboard.app import MainWindow
        window = self._window()
        with patch.object(MainWindow, "_spawn_replacement") as spawn:
            window.btn_quit.click()
            overlay = window._exit.overlay
            self.assertTrue(overlay.isVisible())
            self.assertEqual(overlay.title_label.text(), "Shutting down")
            self.assertEqual(overlay.subtitle_label.text(),
                             "The application is shutting down. Please wait a moment.")
            self.assertEqual(self._states(overlay), ["active", "pending", "pending", "pending"])
            self.assertEqual(overlay.steps[-1].label.text(), "Closing the application")
            # The window stays responsive while the worker stops the engine.
            self.assertTrue(window.isVisible())
            self.assertTrue(wait_until(lambda: self._states(overlay)[1] == "active"))
            self.assertTrue(wait_until(lambda: not window.isVisible(), timeout=10))
        spawn.assert_not_called()
        self.assertTrue(window._exit_done)
        self.assertEqual(self._states(overlay), ["done"] * 4)
        self.assertEqual(overlay.title_label.text(), "Closing now…")
        self.assertFalse(overlay.isVisible())
        self.assertFalse(window.engine.running)
        for worker in (window.engine.camera, window.engine.recognition, window.engine.persistence):
            self.assertFalse(worker.thread.is_alive())
        self.assertFalse(window._sys_monitor.isRunning())

    def test_relaunch_starts_the_new_instance_only_after_the_engine_let_go(self):
        from face_attendance.dashboard import engine as engine_module
        from face_attendance.dashboard.app import MainWindow
        lock_path = Path(self._dir.name) / "attendance.lock"
        spawns = []

        def spawn(window):
            overlay = window._exit.overlay if window._exit is not None else None
            spawns.append({"running": window.engine.running,
                           "lock_held": window.engine._lock.held,
                           "steps": self._states(overlay) if overlay else None})

        with patch.object(engine_module, "LOCK_PATH", lock_path):
            window = self._window(use_lock=True)
        self.assertTrue(window.engine._lock.held)
        with patch.object(MainWindow, "_spawn_replacement", spawn):
            window.btn_relaunch.click()
            overlay = window._exit.overlay
            self.assertEqual(overlay.title_label.text(), "Restarting Face ID Attendance")
            self.assertEqual(overlay.subtitle_label.text(), "The application will restart immediately.")
            self.assertTrue(wait_until(lambda: not window.isVisible(), timeout=10))
        self.assertEqual(spawns, [{"running": False, "lock_held": False, "steps": ["done"] * 4}])

    def test_the_window_close_button_shuts_down_gracefully(self):
        from PyQt6.QtGui import QCloseEvent
        from face_attendance.dashboard.app import MainWindow

        class UserClose(QCloseEvent):
            def spontaneous(self):
                return True

        window = self._window()
        event = UserClose()
        with patch.object(MainWindow, "_spawn_replacement") as spawn:
            window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertTrue(window.isVisible())
            self.assertEqual(window._exit.mode, "quit")
            # A second close while stopping is ignored rather than stopping twice.
            again = UserClose()
            window.closeEvent(again)
            self.assertFalse(again.isAccepted())
            self.assertTrue(wait_until(lambda: not window.isVisible(), timeout=10))
        spawn.assert_not_called()

    def test_programmatic_close_still_stops_synchronously(self):
        window = self._window()
        window.close()
        self.assertFalse(window.isVisible())
        self.assertIsNone(window._exit)
        self.assertFalse(window.engine.running)

    def test_the_card_cannot_be_dismissed(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from face_attendance.dashboard.app import MainWindow
        window = self._window(running=False)
        with patch.object(MainWindow, "_spawn_replacement"):
            window._begin_exit("quit")
            overlay = window._exit.overlay
            overlay.reject()
            QTest.keyClick(overlay, Qt.Key.Key_Escape)
            self.assertTrue(overlay.isVisible())
            window._begin_exit("restart")      # a second request changes nothing
            self.assertEqual(window._exit.mode, "quit")
            self.assertTrue(wait_until(lambda: not window.isVisible(), timeout=10))

    def test_a_failing_engine_stop_still_finishes(self):
        from face_attendance.dashboard.app import MainWindow
        window = self._window(running=False)
        with patch.object(type(window.engine), "shutdown", side_effect=RuntimeError("camera stuck")), \
                patch.object(MainWindow, "_spawn_replacement"):
            window._begin_exit("quit")
            self.assertTrue(wait_until(lambda: not window.isVisible(), timeout=10))
        self.assertTrue(window._exit_done)

    def test_a_slow_stop_says_it_is_still_working(self):
        from face_attendance.dashboard import shutdown
        from face_attendance.dashboard.app import MainWindow
        window = self._window(running=False)
        release = threading.Event()
        real = type(window.engine).shutdown

        def slow(engine, on_stage=None):
            release.wait(5)
            real(engine, on_stage)

        with patch.object(shutdown, "SLOW_AFTER_MS", 50), \
                patch.object(type(window.engine), "shutdown", slow), \
                patch.object(MainWindow, "_spawn_replacement"):
            window._begin_exit("quit")
            overlay = window._exit.overlay
            self.assertTrue(wait_until(lambda: overlay.note.text().startswith("Still finishing up")))
            self.assertTrue(window.isVisible())
            release.set()
            self.assertTrue(wait_until(lambda: not window.isVisible(), timeout=10))

    def test_signing_out_restarts_at_the_sign_in_screen(self):
        from face_attendance import users
        from face_attendance.dashboard.app import MainWindow
        window = self._window(running=False)
        session = Path(self._dir.name) / ".session"
        session.write_text("{}", encoding="utf-8")
        with patch.object(users, "SESSION_PATH", session), \
                patch.object(MainWindow, "_spawn_replacement") as spawn:
            window._logout()
            overlay = window._exit.overlay
            self.assertEqual(overlay.title_label.text(), "Signing out")
            self.assertEqual(overlay.steps[-1].label.text(), "Returning to the sign-in screen")
            self.assertFalse(session.exists())
            self.assertTrue(wait_until(lambda: not window.isVisible(), timeout=10))
        spawn.assert_called_once()


class RelaunchCommandTests(unittest.TestCase):
    def test_source_runs_restart_with_the_original_command_line(self):
        from face_attendance.dashboard.shutdown import relaunch_command
        with patch.object(sys, "orig_argv", ["python3", "-m", "face_attendance.dashboard", "--start"],
                          create=True):
            program, arguments = relaunch_command()
        self.assertEqual(program, sys.executable)
        self.assertEqual(arguments, ["-m", "face_attendance.dashboard", "--start"])

    def test_frozen_builds_do_not_repeat_the_executable(self):
        from face_attendance.dashboard.shutdown import relaunch_command
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", "/Applications/SV Face ID.app/Contents/MacOS/SV Face ID"), \
                patch.object(sys, "argv", ["/Applications/SV Face ID.app/Contents/MacOS/SV Face ID",
                                           "--start"]):
            program, arguments = relaunch_command()
        self.assertEqual(program, "/Applications/SV Face ID.app/Contents/MacOS/SV Face ID")
        self.assertEqual(arguments, ["--start"])


if __name__ == "__main__":
    unittest.main()
