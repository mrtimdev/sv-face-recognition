"""Dashboard shell: navigation, header status, theme switching and shutdown.

The window owns the engine and the screens. Screens only talk to the engine via
signals and are notified of settings changes by ``_broadcast_settings``.
"""
import logging
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                             QMessageBox, QPushButton, QStackedWidget, QStatusBar,
                             QVBoxLayout, QWidget)

from ..settings import SETTINGS_PATH, Settings, describe_source, hash_pin
from .engine import AttendanceEngine
from .screens.employees import EmployeesScreen
from .screens.live import LiveScreen
from .screens.realtime import RealtimeScreen
from .screens.report import ReportScreen
from .screens.settings import SettingsScreen
from .telegram import TelegramService
from .theme import stylesheet
from .widgets import StatusPill


NAV = (
    ("\U0001F4FA", "Live Monitor"),
    ("\U0001F4CB", "Real-time Attendance"),
    ("\U0001F4CA", "Attendance Report"),
    ("\U0001F465", "Employees"),
    ("⚙️", "Settings"),
)


class LoginDialog(QDialog):
    """Blocks dashboard access until the correct PIN is entered."""

    def __init__(self, pin_hash, parent=None):
        super().__init__(parent)
        self._pin_hash = pin_hash
        self._attempts = 0
        self.setWindowTitle("Face ID Attendance - Login")
        self.setFixedSize(360, 180)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(QLabel("Enter the dashboard PIN to continue:"))
        self._pin = QLineEdit()
        self._pin.setEchoMode(QLineEdit.EchoMode.Password)
        self._pin.setPlaceholderText("PIN")
        self._pin.returnPressed.connect(self._check)
        layout.addWidget(self._pin)
        self._message = QLabel("")
        layout.addWidget(self._message)
        self._button = QPushButton("Unlock")
        self._button.clicked.connect(self._check)
        layout.addWidget(self._button)

    def _check(self):
        if hash_pin(self._pin.text()) == self._pin_hash:
            self.accept()
            return
        self._attempts += 1
        remaining = max(0, 5 - self._attempts)
        if remaining == 0:
            self._message.setText("Too many failed attempts.")
            self.reject()
            return
        self._message.setText(f"Incorrect PIN. {remaining} attempt(s) remaining.")
        self._pin.clear()
        self._pin.setFocus()


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
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header bar ──────────────────────────────────────────────
        header = QFrame()
        header.setObjectName("headerBar")
        header.setFixedHeight(64)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        hl.setSpacing(16)

        welcome_col = QVBoxLayout()
        welcome_col.setSpacing(0)
        self.welcome_label = QLabel("Welcome back, Admin")
        self.welcome_label.setObjectName("welcomeText")
        self.welcome_sub = QLabel("Face ID Attendance System")
        self.welcome_sub.setObjectName("welcomeSub")
        welcome_col.addWidget(self.welcome_label)
        welcome_col.addWidget(self.welcome_sub)
        hl.addLayout(welcome_col)
        hl.addStretch(1)

        self.engine_pill = StatusPill("STOPPED", "idle")
        hl.addWidget(self.engine_pill)

        self.notif_label = QLabel("\U0001F514")
        self.notif_label.setToolTip("Notifications")
        self.notif_label.setStyleSheet("font-size: 18px; background: transparent;")
        hl.addWidget(self.notif_label)

        clock_col = QVBoxLayout()
        clock_col.setSpacing(0)
        clock_col.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.clock_label = QLabel("")
        self.clock_label.setObjectName("headerClock")
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.date_label = QLabel("")
        self.date_label.setObjectName("headerDate")
        self.date_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col.addWidget(self.clock_label)
        clock_col.addWidget(self.date_label)
        hl.addLayout(clock_col)

        avatar = QLabel("\U0001F464")
        avatar.setStyleSheet(
            "font-size: 24px; background: #E2E8F0; border-radius: 18px; "
            "padding: 4px 8px; min-width: 36px; min-height: 36px;")
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hl.addWidget(avatar)
        root.addWidget(header)

        # ── Body: sidebar + content ─────────────────────────────────
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # Sidebar
        sidebar = QVBoxLayout()
        sidebar.setContentsMargins(0, 0, 0, 0)
        sidebar.setSpacing(0)

        logo_frame = QWidget()
        logo_lay = QVBoxLayout(logo_frame)
        logo_lay.setContentsMargins(16, 16, 16, 8)
        logo_lay.setSpacing(4)
        logo = QLabel("SV Face ID")
        logo.setObjectName("appTitle")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        online_label = QLabel("● Online")
        online_label.setStyleSheet("color: #16A34A; font-size: 12px; font-weight: 600;")
        online_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_lay.addWidget(logo)
        logo_lay.addWidget(online_label)
        self._online_label = online_label
        sidebar.addWidget(logo_frame)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(220)
        for icon, name in NAV:
            self.nav.addItem(QListWidgetItem(f"{icon}  {name}"))
        sidebar.addWidget(self.nav, 1)

        # Sidebar footer
        footer_frame = QFrame()
        footer_frame.setObjectName("sidebarFooter")
        footer_frame.setFixedWidth(220)
        footer_lay = QVBoxLayout(footer_frame)
        footer_lay.setContentsMargins(14, 10, 14, 10)
        footer_lay.setSpacing(4)
        self.source_pill = StatusPill("-", "idle")
        footer_lay.addWidget(self.source_pill)
        self.sys_status_label = QLabel("✅ All Systems Operational")
        self.sys_status_label.setObjectName("muted")
        self.sys_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_lay.addWidget(self.sys_status_label)
        version = QLabel("v1.0.0  © 2026 SV Technologies")
        version.setObjectName("versionLabel")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_lay.addWidget(version)
        sidebar.addWidget(footer_frame)
        body.addLayout(sidebar)

        # Content stack
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
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(
            "Blink-based liveness detection is active.  "
            "Attendance is persisted to SQLite with WAL journaling.")
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
        self.engine_pill.set_status("RUNNING" if running else "STOPPED",
                                    "ok" if running else "idle")
        if running:
            self._online_label.setText("● Online")
            self._online_label.setStyleSheet("color: #16A34A; font-size: 12px; font-weight: 600;")
            self.sys_status_label.setText("✅ All Systems Operational")
        else:
            self._online_label.setText("● Offline")
            self._online_label.setStyleSheet("color: #DC2626; font-size: 12px; font-weight: 600;")
            self.sys_status_label.setText("⚠️ Engine Stopped")
        self._refresh_header()

    def _refresh_header(self):
        self.source_pill.set_status(describe_source(self.settings.source), "info")
        try:
            enrolled = self.engine.catalog.enrolled_count
        except Exception:
            enrolled = 0
        self.welcome_sub.setText(
            f"{enrolled} employee(s) enrolled  •  Face ID Attendance System")

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
        if self.engine.running:
            self.engine.persistence.log_audit("settings_changed",
                                              "restart" if restart else "no_restart")

    def apply_theme(self, theme):
        application = QApplication.instance()
        if application is not None:
            application.setStyleSheet(stylesheet(theme))
        for screen in getattr(self, "screens", []):
            setter = getattr(screen, "set_theme", None)
            if callable(setter):
                setter(theme)

    def _tick(self):
        now = datetime.now()
        self.clock_label.setText(now.strftime("%H:%M:%S"))
        self.date_label.setText(now.strftime("%a %d %b %Y"))

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
    if settings.dashboard_pin_hash:
        dialog = LoginDialog(settings.dashboard_pin_hash)
        application.setStyleSheet(stylesheet(settings.theme))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return 1
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
