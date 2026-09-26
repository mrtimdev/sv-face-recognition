import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests.test_dashboard import _app, temp_settings


class DashboardErrorTests(unittest.TestCase):
    def test_queued_callback_error_does_not_abort_python(self):
        # Exercise PyQt's actual exception boundary in another process. A
        # direct call to the handler would not test the reported SIGABRT.
        script = """
import sys
from PyQt6.QtCore import QObject, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication
from face_attendance.dashboard.errors import DashboardErrorHandler
app = QApplication([])
handler = DashboardErrorHandler(sys.argv[1])
handler.install()
seen = []
handler.errorRaised.connect(seen.append)
class Sender(QObject):
    fired = pyqtSignal()
sender = Sender()
def fail():
    raise RuntimeError('queued callback regression')
sender.fired.connect(fail, Qt.ConnectionType.QueuedConnection)
QTimer.singleShot(0, sender.fired.emit)
QTimer.singleShot(50, sender.fired.emit)
QTimer.singleShot(150, app.quit)
app.exec()
handler.close()
assert len(seen) == 1, seen
print('EVENT_LOOP_SURVIVED')
"""
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "errors.log"
            result = subprocess.run([sys.executable, "-B", "-c", script, str(log)],
                                    env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("EVENT_LOOP_SURVIVED", result.stdout)
            content = log.read_text()
            self.assertIn("Traceback (most recent call last)", content)
            self.assertEqual(content.count("RuntimeError: queued callback regression"), 1)

    def test_readonly_log_directory_still_reports_and_restores_hook(self):
        from face_attendance.dashboard.errors import DashboardErrorHandler
        _app()
        previous = sys.excepthook
        with patch("face_attendance.dashboard.errors.os.open", side_effect=PermissionError):
            handler = DashboardErrorHandler("/tmp/sv-denied-errors.log")
        seen = []
        handler.errorRaised.connect(seen.append)
        handler.install()
        try:
            handler.handle_exception(ValueError, ValueError("test error"), None)
            self.assertFalse(handler.file_available)
            self.assertIn("launch terminal", seen[0])
        finally:
            handler.close()
        self.assertIs(sys.excepthook, previous)

    def test_window_pauses_recording_and_keeps_error_details_available(self):
        from PyQt6.QtWidgets import QMessageBox
        from face_attendance.dashboard.app import MainWindow
        app = _app()
        with tempfile.TemporaryDirectory() as directory:
            window = MainWindow(temp_settings(directory), use_lock=False)
            window.show()
            try:
                window.on_unhandled_error("RuntimeError: test\nDetails: local error log")
                app.processEvents()
                self.assertTrue(window.engine.paused)
                self.assertTrue(window.isVisible())
                dialogs = window.findChildren(QMessageBox)
                self.assertEqual(len(dialogs), 1)
                self.assertIn("local error log", dialogs[0].detailedText())
                dialogs[0].accept()
            finally:
                window.close()
                app.processEvents()
