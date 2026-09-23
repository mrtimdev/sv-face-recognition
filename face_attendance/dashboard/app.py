"""Dashboard shell: navigation, header status, theme switching and shutdown.

The window owns the engine and the screens. Screens only talk to the engine via
signals and are notified of settings changes by ``_broadcast_settings``.
"""
import logging
from datetime import datetime

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
                             QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                             QMessageBox, QPushButton, QStackedWidget, QStatusBar,
                             QVBoxLayout, QWidget)

from ..settings import SETTINGS_PATH, Settings, describe_source, hash_pin
from .engine import AttendanceEngine
from .icons import IconLabel, apply_button_icon, make_icon
from .screens.employees import EmployeesScreen
from .screens.live import LiveScreen
from .screens.realtime import RealtimeScreen
from .screens.report import ReportScreen
from .screens.settings import SettingsScreen
from .telegram import TelegramService
from .theme import palette, stylesheet
from .widgets import Avatar, StatusPill

APP_VERSION = "v2.0.0"
SIDEBAR_WIDTH = 244

NAV = (
    ("monitor", "Live Monitor"),
    ("list", "Real-time Attendance"),
    ("chart", "Attendance Report"),
    ("users", "Enrolled Employees"),
    ("gear", "Settings"),
)


class NotificationButton(QPushButton):
    """Round icon button that can carry an unread-count bubble."""

    def __init__(self, icon_name="bell", parent=None):
        super().__init__(parent)
        self.setObjectName("headerIconButton")
        self.setFixedSize(42, 42)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._icon_label = IconLabel(icon_name, 20, palette("light")["muted"], parent=self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._icon_label, 0, Qt.AlignmentFlag.AlignCenter)
        self.badge = QLabel("", self)
        self.badge.setObjectName("badge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setVisible(False)

    def set_icon_name(self, name, color):
        self._icon_label.set_icon(name)
        self._icon_label.set_icon_color(color)

    def set_count(self, count):
        count = int(count or 0)
        self.badge.setText(str(count) if count < 100 else "99+")
        self.badge.setVisible(count > 0)
        self._place_badge()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_badge()

    def _place_badge(self):
        width = max(16, self.badge.fontMetrics().horizontalAdvance(self.badge.text()) + 8)
        self.badge.setFixedSize(width, 16)
        self.badge.move(self.width() - width - 5, 3)


class LoginDialog(QDialog):
    """Blocks dashboard access until the correct PIN is entered."""

    def __init__(self, pin_hash, parent=None):
        super().__init__(parent)
        self._pin_hash = pin_hash
        self._attempts = 0
        self.setWindowTitle("Face ID Attendance - Login")
        self.setFixedSize(400, 320)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        card = QFrame()
        card.setObjectName("loginCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 26, 24, 22)
        layout.setSpacing(10)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        tile = QFrame()
        tile.setObjectName("brandTile")
        tile.setFixedSize(40, 40)
        tile_layout = QHBoxLayout(tile)
        tile_layout.setContentsMargins(0, 0, 0, 0)
        self._brand_icon = IconLabel("face-id", 22, "#FFFFFF")
        tile_layout.addWidget(self._brand_icon, 0, Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(tile)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_title = QLabel("Face ID Attendance")
        brand_title.setObjectName("appTitle")
        brand_note = QLabel("Administrator sign-in")
        brand_note.setObjectName("brandMark")
        brand_text.addWidget(brand_title)
        brand_text.addWidget(brand_note)
        brand_row.addLayout(brand_text)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)
        layout.addSpacing(6)

        title = QLabel("Unlock the dashboard")
        title.setObjectName("loginTitle")
        layout.addWidget(title)
        hint = QLabel("Enter the dashboard PIN to continue.")
        hint.setObjectName("emptyBody")
        layout.addWidget(hint)
        layout.addSpacing(4)

        self._pin = QLineEdit()
        self._pin.setEchoMode(QLineEdit.EchoMode.Password)
        self._pin.setPlaceholderText("PIN")
        self._pin.setMinimumHeight(38)
        self._pin.returnPressed.connect(self._check)
        layout.addWidget(self._pin)
        self._message = QLabel("")
        self._message.setObjectName("emptyBody")
        self._message.setWordWrap(True)
        layout.addWidget(self._message)
        layout.addStretch(1)

        self._button = QPushButton("Unlock dashboard")
        self._button.setObjectName("primary")
        self._button.setMinimumHeight(38)
        self._button.clicked.connect(self._check)
        layout.addWidget(self._button)
        outer.addWidget(card)
        self._pin.setFocus()

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
        self.resize(1280, 800)
        self.setMinimumSize(1100, 680)
        self._pending_notifications = 0
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

    # ── construction ───────────────────────────────────────────────────────

    def _build(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())

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
            "Look at the camera, then blink once or smile.  "
            "Keep your whole face visible.")
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self._refresh_header()

    def _build_header(self):
        header = QFrame()
        header.setObjectName("headerBar")
        header.setFixedHeight(56)
        row = QHBoxLayout(header)
        row.setContentsMargins(22, 0, 22, 0)
        row.setSpacing(14)

        greeting = QVBoxLayout()
        greeting.setSpacing(1)
        self.welcome_label = QLabel("\U0001F44B  Welcome back, Admin")
        self.welcome_label.setObjectName("welcomeText")
        self.welcome_sub = QLabel("Face ID Attendance System")
        self.welcome_sub.setObjectName("welcomeSub")
        greeting.addWidget(self.welcome_label)
        greeting.addWidget(self.welcome_sub)
        row.addLayout(greeting)
        row.addStretch(1)

        self.engine_pill = StatusPill("STOPPED", "idle", dot=True)
        row.addWidget(self.engine_pill)

        self.date_label = QLabel("")
        self.date_label.setObjectName("headerChipSecondary")
        row.addWidget(self.date_label)

        self.clock_label = QLabel("")
        self.clock_label.setObjectName("headerClock")
        row.addWidget(self.clock_label)

        self.notif_button = NotificationButton("bell")
        self.notif_button.setToolTip("Notifications")
        self.notif_button.clicked.connect(self._show_notifications)
        row.addWidget(self.notif_button)

        self.avatar = Avatar("Admin", size=36)
        row.addWidget(self.avatar)
        return header

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)

        column = QVBoxLayout(sidebar)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        # ── brand ───────────────────────────────────────────────────────
        brand = QWidget()
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(18, 20, 18, 14)
        brand_row.setSpacing(12)
        self.brand_tile = QFrame()
        self.brand_tile.setObjectName("brandTile")
        self.brand_tile.setFixedSize(42, 42)
        tile_layout = QHBoxLayout(self.brand_tile)
        tile_layout.setContentsMargins(0, 0, 0, 0)
        self.brand_icon = IconLabel("face-id", 24, "#FFFFFF")
        tile_layout.addWidget(self.brand_icon, 0, Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(self.brand_tile)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        brand_title = QLabel("Face ID\nAttendance")
        brand_title.setObjectName("appTitle")
        self._online_label = QLabel("\u25cf Offline")
        self._online_label.setObjectName("onlineStatus")
        self._online_label.setProperty("offline", True)
        brand_text.addWidget(brand_title)
        brand_text.addWidget(self._online_label)
        brand_row.addLayout(brand_text)
        brand_row.addStretch(1)
        column.addWidget(brand)

        # ── navigation ──────────────────────────────────────────────────
        section = QLabel("MAIN MENU")
        section.setObjectName("brandMark")
        section.setContentsMargins(28, 8, 18, 6)
        column.addWidget(section)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setSpacing(2)
        for icon_name, label in NAV:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, icon_name)
            self.nav.addItem(item)
        column.addWidget(self.nav, 1)

        # ── footer: system status + version ─────────────────────────────
        footer = QFrame()
        footer.setObjectName("sidebarFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(16, 14, 16, 16)
        footer_layout.setSpacing(10)

        status_card = QFrame()
        status_card.setObjectName("subtlePanel")
        status_layout = QHBoxLayout(status_card)
        status_layout.setContentsMargins(12, 10, 12, 10)
        status_layout.setSpacing(10)
        self.sys_status_dot = IconLabel("dot", 12)
        status_layout.addWidget(self.sys_status_dot)
        status_text = QVBoxLayout()
        status_text.setSpacing(0)
        status_title = QLabel("System Status")
        status_title.setObjectName("cardTitle")
        self.sys_status_label = QLabel("All Systems Operational")
        self.sys_status_label.setObjectName("emptyBody")
        status_text.addWidget(status_title)
        status_text.addWidget(self.sys_status_label)
        status_layout.addLayout(status_text)
        status_layout.addStretch(1)
        footer_layout.addWidget(status_card)

        version = QLabel(f"{APP_VERSION}\n\u00a9 2026 SV Technologies")
        version.setObjectName("versionLabel")
        version.setContentsMargins(4, 0, 0, 0)
        footer_layout.addWidget(version)
        tagline = QLabel("Secure \u2022 Accurate \u2022 Smarter")
        tagline.setObjectName("legalLabel")
        tagline.setContentsMargins(4, 0, 0, 0)
        footer_layout.addWidget(tagline)
        column.addWidget(footer)
        return sidebar

    def _connect(self):
        self.engine.runningChanged.connect(self._engine_state)
        live = self.screens[0]
        live.viewAllRequested.connect(lambda: self.nav.setCurrentRow(1))
        live.settingsRequested.connect(lambda: self.nav.setCurrentRow(4))
        ctx = self.screens[4]
        ctx.settingsApplied.connect(
            lambda settings, restart: self._broadcast_settings(settings, restart))
        self.nav.currentRowChanged.connect(lambda row: self._refresh_nav_icons())
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._tick)
        self.clock_timer.start()
        self._tick()

    # --- state propagation ------------------------------------------------
    def _engine_state(self, running):
        self.engine_pill.set_status("RUNNING" if running else "STOPPED",
                                    "ok" if running else "idle")
        colors = palette(self.settings.theme)
        if running:
            self._online_label.setText("\u25cf Online")
            self.sys_status_label.setText("All Systems Operational")
            self.sys_status_dot.set_icon_color(colors["success"])
        else:
            self._online_label.setText("\u25cf Offline")
            self.sys_status_label.setText("Engine Stopped")
            self.sys_status_dot.set_icon_color(colors["muted"])
        self._online_label.setProperty("offline", not running)
        self._online_label.style().unpolish(self._online_label)
        self._online_label.style().polish(self._online_label)
        self._refresh_header()

    def _refresh_header(self):
        try:
            enrolled = self.engine.catalog.enrolled_count
        except Exception:
            enrolled = 0
        self.welcome_sub.setText(
            f"{enrolled} employee(s) enrolled  \u2022  {describe_source(self.settings.source)}")

    def _refresh_nav_icons(self):
        colors = palette(self.settings.theme)
        current = self.nav.currentRow()
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            icon_name = item.data(Qt.ItemDataRole.UserRole) or "dot"
            color = colors["nav_icon_active"] if row == current else colors["nav_icon"]
            item.setIcon(make_icon(icon_name, 18, color, 1.7))

    def _refresh_chrome(self):
        """Re-tint every hand-painted glyph after a theme switch."""
        colors = palette(self.settings.theme)
        self.avatar.set_theme(self.settings.theme)
        self.notif_button.set_icon_name("bell", colors["text_secondary"])
        self.notif_button.badge.setStyleSheet(
            f"background-color: {colors['badge_bg']}; color: {colors['badge_fg']};"
            "border-radius: 8px; font-size: 10px; font-weight: 700;")
        self._refresh_nav_icons()
        self._engine_state(self.engine.running)

    def notify(self, count=1):
        """Bump the notification badge in the header."""
        self._pending_notifications += count
        self.notif_button.set_count(self._pending_notifications)

    def _show_notifications(self):
        if not self._pending_notifications:
            self._note("No new notifications.")
            return
        self._note(f"{self._pending_notifications} new event(s) since the last check.")
        self._pending_notifications = 0
        self.notif_button.set_count(0)

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
        if hasattr(self, "avatar"):
            self._refresh_chrome()

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
        self.notify(1)
        self.telegram.send_capture(
            employee_name=job.employee_name,
            employee_id=job.employee_id,
            duration=job.duration,
            frame=job.frame,
        )

    def _telegram_on_unknown(self, info):
        self.notify(1)
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
