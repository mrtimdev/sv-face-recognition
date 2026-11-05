"""Report uncaught Qt callback exceptions without PyQt aborting the process."""
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import traceback

from PyQt6.QtCore import QObject, pyqtSignal


class DashboardErrorHandler(QObject):
    errorRaised = pyqtSignal(str)

    def __init__(self, log_path, parent=None):
        super().__init__(parent)
        self.log_path = Path(log_path)
        self._previous = None
        self._handling = False
        self._reported = set()
        self._logger = logging.Logger("dashboard-errors", level=logging.ERROR)
        self._logger.propagate = False
        self._logger.addHandler(logging.StreamHandler(sys.stderr))
        self.file_available = False
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(self.log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            os.close(descriptor)
            handler = RotatingFileHandler(self.log_path, maxBytes=1_000_000,
                                          backupCount=2, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._logger.addHandler(handler)
            self.file_available = True
        except OSError:
            # A read-only/full disk must not turn error reporting into a crash.
            self._logger.error("Cannot write dashboard error log; using terminal output")
        # Keep the exact bound method object so uninstall can identify our hook.
        self._hook = self.handle_exception

    def install(self):
        if self._previous is None:
            self._previous = sys.excepthook
            sys.excepthook = self._hook

    def handle_exception(self, kind, value, tb):
        if self._handling:
            return
        self._handling = True
        try:
            frames = traceback.extract_tb(tb)
            location = (frames[-1].filename, frames[-1].lineno) if frames else None
            key = (kind, location)
            # A failing preview/timer callback can fire dozens of times a
            # second. Log/notify once per failure location until next launch.
            if key in self._reported:
                return
            if len(self._reported) >= 100:
                self._reported.clear()
            self._reported.add(key)
            self._logger.error("Unhandled dashboard callback", exc_info=(kind, value, tb))
            destination = str(self.log_path) if self.file_available else "the launch terminal"
            self.errorRaised.emit(f"{kind.__name__}: {value}\nDetails: {destination}")
        except Exception:
            # The exception hook itself must never propagate into Qt.
            try:
                traceback.print_exception(kind, value, tb, file=sys.stderr)
            except Exception:
                pass
        finally:
            self._handling = False

    def close(self):
        if sys.excepthook is self._hook:
            sys.excepthook = self._previous
        self._previous = None
        for handler in self._logger.handlers[:]:
            self._logger.removeHandler(handler)
            handler.close()
