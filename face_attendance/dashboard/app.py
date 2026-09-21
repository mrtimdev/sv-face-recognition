"""Dashboard shell: navigation, header status, theme switching and shutdown.

The window owns the engine and the screens. Screens only talk to the engine via
signals and are notified of settings changes by ``_broadcast_settings``.
"""
import logging

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                             QMainWindow, QMessageBox, QStackedWidget, QStatusBar,
                             QVBoxLayout, QWidget)

from ..settings import SETTINGS_PATH, Settings, describe_source
from .engine import AttendanceEngine
from .screens.employees import EmployeesScreen
from .screens.live import LiveScreen
from .screens.realtime import RealtimeScreen
from .screens.report import ReportScreen
from .screens.settings import SettingsScreen
from .telegram import TelegramService
from .theme import stylesheet
from .widgets import StatusPill


NAV = ("Live Monitor", "Real-time Attendance", "Attendance Report", "Employees", "Settings")


class MainWindow(QMainWindow):
    def __init__(self, settings, backend=None, capture_factory=None, use_lock=True,
                 autostart=False):
        super().__init__()
        self.settings = settings
        self.engine = AttendanceEngine(settings, self, backend=backend,
                                      capture_factory=capture_factory, use_lock=use_lock)
        self.telegram = TelegramService(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            notify_capture=settings.telegram_notify_capture,
            notify_unknown=settings.telegram_notify_unknown,
        )
        self.setWindowTitle("Face ID Attendance - Admin Dashboard")
        self.resize(1500, 950)
        self.setMinimumSize(1100, 720)
        self._build()
        self._connect()
        self.apply_theme(settings.theme)
        self.engine.logMessage.connect(self._note)
        self.engine.errorRaised.connect(lambda message: self._note(f"ERROR: {message}"))
        self.engine.catalogChanged.connect(lambda rows: self._refresh_header())
        self.engine.attendanceSaved.connect(self._telegram_on_saved)
        self.engine.unknownFaceAlert.connect(self._telegram_on_unknown)
        if autostart:
            QTimer.singleShot(0, self._autostart)

    def _build(self):
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(210)
        for name in NAV:
            self.nav.addItem(QListWidgetItem(name))
        layout.addWidget(self.nav)

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 12, 18, 12)
        titles = QVBoxLayout()
        title = QLabel("Face ID Attendance")
        title.setObjectName("appTitle")
        self.header_subtitle = QLabel("")
        self.header_subtitle.setObjectName("screenSubtitle")
        titles.addWidget(title)
        titles.addWidget(self.header_subtitle)
        header_layout.addLayout(titles)
        header_layout.addStretch(1)
        self.engine_pill = StatusPill("STOPPED", "idle")
        self.source_pill = StatusPill("-", "idle")
        self.clock_label = QLabel("")
        self.clock_label.setObjectName("muted")
        header_layout.addWidget(self.source_pill)
        header_layout.addWidget(self.engine_pill)
        header_layout.addWidget(self.clock_label)
        right.addWidget(header)

        self.stack = QStackedWidget()
        self.screens = [
            LiveScreen(self.engine, self.settings),
            RealtimeScreen(self.engine, self.settings),
            ReportScreen(self.engine, self.settings),
            EmployeesScreen(self.engine, self.settings),
            SettingsScreen(self.engine, self.settings),
        ]
        for screen in self.screens:
            self.stack.addWidget(screen)
        right.addWidget(self.stack, 1)
        layout.addLayout(right, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(
            "Attendance is written to SQLite; the CSV log is observation telemetry only. "
            "This build does not implement anti-spoofing/liveness detection.")
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self._refresh_header()

    def _connect(self):
        self.engine.runningChanged.connect(self._engine_state)
        settings_screen = self.screens[4]
        settings_screen.settingsApplied.connect(
            lambda settings, restart: self._broadcast_settings(settings, restart))
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._tick)
        self.clock_timer.start()
        self._tick()

    # --- state propagation ------------------------------------------------
    def _engine_state(self, running):
        self.engine_pill.set_status("RUNNING" if running else "STOPPED", "ok" if running else "idle")
        self._refresh_header()

    def _refresh_header(self):
        self.source_pill.set_status(describe_source(self.settings.source), "info")
        try:
            enrolled = self.engine.catalog.enrolled_count
        except Exception:
            enrolled = 0
        self.header_subtitle.setText(f"{enrolled} employee(s) enrolled  |  "
                                     f"recording pauses automatically when the camera drops")

    def _broadcast_settings(self, settings, restart):
        self.settings = settings
        self.apply_theme(settings.theme)
        self.telegram.reconfigure(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            notify_capture=settings.telegram_notify_capture,
            notify_unknown=settings.telegram_notify_unknown,
        )
        for screen in self.screens:
            handler = getattr(screen, "on_settings_changed", None)
            if callable(handler):
                handler(settings)
        self._refresh_header()
        self._note(f"Settings saved{'' if restart else ' (restart the engine to apply)'}")

    def apply_theme(self, theme):
        application = QApplication.instance()
        if application is not None:
            application.setStyleSheet(stylesheet(theme))
        for screen in getattr(self, "screens", []):
            setter = getattr(screen, "set_theme", None)
            if callable(setter):
                setter(theme)

    def _tick(self):
        from datetime import datetime
        self.clock_label.setText(datetime.now().strftime("%a %d %b  %H:%M:%S"))

    def _note(self, message):
        logging.info("dashboard: %s", message)
        self.statusBar().showMessage(message, 8000)

    # --- telegram handlers ------------------------------------------------
    def _telegram_on_saved(self, result):
        job = result.job
        self.telegram.send_capture(
            employee_name=job.employee_name,
            employee_id=job.employee_id,
            duration=job.duration,
            frame=job.frame,
        )

    def _telegram_on_unknown(self, info):
        self.telegram.send_unknown_alert(
            track_id=info["track_id"],
            age=info["age"],
            frame=info.get("frame"),
        )

    def _autostart(self):
        try:
            self.engine.start()
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "Cannot start the engine", str(exc))
            self.statusBar().showMessage(str(exc))

    # --- shutdown ---------------------------------------------------------
    def closeEvent(self, event):
        self.clock_timer.stop()
        self.telegram.stop()
        self.engine.shutdown()
        # Closing each screen stops its timers and closes read-only connections.
        for screen in self.screens:
            try:
                screen.close()
            except Exception:
                logging.debug("screen cleanup failed", exc_info=True)
        super().closeEvent(event)


def run_dashboard(settings_path=None, autostart=False, argv=None):
    """Create the application, show the window and block until it closes."""
    import sys
    application = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    application.setApplicationName("Face ID Attendance Dashboard")
    settings = Settings.load(settings_path)
    window = MainWindow(settings, autostart=autostart)
    window.show()
    return application.exec()


def main(argv=None):
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Face ID attendance admin dashboard (PyQt6)")
    parser.add_argument("--settings", default=None, help=f"Settings file (default {SETTINGS_PATH})")
    parser.add_argument("--start", action="store_true",
                        help="Open the camera and record attendance as soon as the window opens")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    return run_dashboard(args.settings, autostart=args.start, argv=sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())