"""Dashboard shell: navigation, header status, theme switching and shutdown.

The window owns the engine and the screens. Screens only talk to the engine via
signals and are notified of settings changes by ``_broadcast_settings``.
"""
import logging
import os
from datetime import datetime

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtWidgets import (QApplication, QDialog, QFrame, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QMainWindow,
                             QMessageBox, QPushButton, QSizePolicy,
                             QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from .. import updates
from ..settings import SETTINGS_PATH, Settings, describe_source
from ..version import __version__
from ..config import ROOT
from ..storage import context_path
from .access import AccessControl, AccountWatcher, NAV_PERMISSIONS, guard
from .bridge import to_pixmap
from .engine import AttendanceEngine
from .errors import DashboardErrorHandler
from .icons import IconLabel, apply_button_icon, make_icon
from .login import LoginDialog, UserLoginDialog
from .screens.employees import EmployeesScreen
from .screens.live import LiveScreen
from .screens.report import ReportScreen
from .screens.settings import SettingsScreen
from .screens.usage import UsageScreen
from .screens.updates import UpdatePromptDialog
from .screens.users import UsersScreen, ProfileDialog
from .shutdown import ShutdownSequence, relaunch_command
from .sysmon import Monitor as SysMonitor, Snapshot as SysSnapshot
from .telegram import TelegramService
from .theme import palette, stylesheet
from .widgets import (IconTile, NotificationButton, NotificationCenter, NotificationPanel,
                      StatusPill, UserChip, UserMenuPanel)
from .splash import SplashScreen
from .widgets.activity_detail import load_photo
from .widgets.capture_flash import CaptureFlash
from .widgets.user_menu import role_title

APP_VERSION = f"v{__version__}"
UPDATE_CHECK_DELAY_MS = 4000
SIDEBAR_WIDTH = 244

NAV = (
    ("monitor", "Live Monitor"),
    ("chart", "Attendance Report"),
    ("users", "Enrolled Employees"),
    ("bolt", "Live Usage"),
    ("shield", "Users & Access"),
    ("gear", "Settings"),
)


class HeaderDetails(QLabel):
    """Keep long camera sources within the header, with full details on hover."""

    def __init__(self):
        super().__init__()
        self._full_text = ""
        self.setObjectName("headerDetails")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text):
        self._full_text = text
        self.setToolTip(text)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, self.contentsRect().width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class MainWindow(QMainWindow):
    def __init__(self, settings, backend=None, capture_factory=None, use_lock=True,
                 autostart=False, current_user=None):
        super().__init__()
        self.settings = settings
        self.current_user = current_user
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
        self.notifications = NotificationCenter(self)
        self.access = AccessControl(current_user, settings.theme, self)
        self._last_row = -1
        self._exit = None
        self._exit_done = False
        self._pending_install = None
        self._update_prompt = None
        self._prompt_for_update = False
        self._notified_version = ""
        self._build()
        self.access.changed.connect(self._apply_access)
        self.capture_flash = CaptureFlash(self.centralWidget())
        self._connect()
        self.apply_theme(settings.theme)
        self.account_watcher = None
        if current_user is not None:
            self.account_watcher = AccountWatcher(settings, current_user, self)
            self.account_watcher.changed.connect(self._apply_account)
            self.account_watcher.start()
        self.engine.logMessage.connect(self._note)
        self.engine.errorRaised.connect(lambda message: self._note(f"ERROR: {message}"))
        self.engine.catalogChanged.connect(lambda rows: self._refresh_header())
        self.engine.attendanceSaved.connect(self._telegram_on_saved)
        self.engine.unknownFaceAlert.connect(self._telegram_on_unknown)
        self.engine.attendanceSaved.connect(self._notify_saved)
        self.engine.attendanceFailed.connect(self._notify_failed)
        self.engine.unknownFaceAlert.connect(self._notify_unknown)
        self.engine.errorRaised.connect(self._notify_error)
        self.notifications.changed.connect(self._on_notifications_changed)
        if updates.installed_location() is not None:
            # Only an installed app updates itself; source runs (and tests) never phone home.
            try:
                updates.remove_stale_downloads()
            except OSError:
                logging.debug("could not tidy old update downloads", exc_info=True)
            if settings.update_auto_check:
                QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._auto_check_updates)
        if autostart:
            QTimer.singleShot(0, self._autostart)

    # ── construction ───────────────────────────────────────────────────────

    def _build(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = self._build_header()
        root.addWidget(self.header)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.sidebar = self._build_sidebar()
        body.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        self.screens = [
            LiveScreen(self.engine, self.settings),
            ReportScreen(self.engine, self.settings),
            EmployeesScreen(self.engine, self.settings),
            UsageScreen(self.engine, self.settings),
            UsersScreen(self.engine, self.settings, current_user=self.current_user),
            SettingsScreen(self.engine, self.settings),
        ]
        for screen in self.screens:
            self.stack.addWidget(screen)
        self.no_access_page = self._build_no_access_page()
        self.stack.addWidget(self.no_access_page)
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        self.setCentralWidget(central)
        self._build_status_bar()
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        self._apply_access()
        self._refresh_header()

    def _build_header(self):
        header = QFrame()
        header.setObjectName("headerBar")
        header.setFixedHeight(84)
        self.header_bar = header
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 20, 0)
        row.setSpacing(20)

        brand = QFrame()
        brand.setObjectName("headerBrand")
        brand.setFixedWidth(SIDEBAR_WIDTH)
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(18, 0, 18, 0)
        brand_row.setSpacing(12)
        self.brand_tile = QFrame()
        self.brand_tile.setObjectName("brandTile")
        self.brand_tile.setFixedSize(40, 40)
        tile_layout = QHBoxLayout(self.brand_tile)
        tile_layout.setContentsMargins(0, 0, 0, 0)
        self.brand_icon = IconLabel("face-id", 24, "#FFFFFF")
        tile_layout.addWidget(self.brand_icon, 0, Qt.AlignmentFlag.AlignCenter)
        brand_row.addWidget(self.brand_tile)
        brand_title = QLabel("Face ID\nAttendance")
        brand_title.setObjectName("appTitle")
        brand_row.addWidget(brand_title, 1)
        row.addWidget(brand)

        page_info = QVBoxLayout()
        page_info.setSpacing(4)
        page_info.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.page_title = QLabel("Live Monitor")
        self.page_title.setObjectName("pageTitle")
        self.header_details = HeaderDetails()
        page_info.addWidget(self.page_title)
        page_info.addWidget(self.header_details)
        row.addLayout(page_info, 1)

        self.engine_pill = StatusPill("ENGINE STOPPED", "idle", dot=True)
        self.engine_pill.setFixedHeight(30)
        row.addWidget(self.engine_pill, 0, Qt.AlignmentFlag.AlignVCenter)

        clock = QVBoxLayout()
        clock.setSpacing(2)
        clock.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.date_label = QLabel("")
        self.date_label.setObjectName("headerChipSecondary")
        self.date_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        self.clock_label = QLabel("")
        self.clock_label.setObjectName("headerClock")
        self.clock_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock.addWidget(self.clock_label)
        clock.addWidget(self.date_label)
        row.addLayout(clock)

        # ── notifications + account ──────────────────────────────────────
        account = QHBoxLayout()
        account.setSpacing(10)
        theme = self.settings.theme
        self.notif_button = NotificationButton(theme)
        self.notif_button.clicked.connect(self._toggle_notifications)
        account.addWidget(self.notif_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.notification_panel = NotificationPanel(self.notifications, theme, self)
        self.notification_panel.activated.connect(self._open_notification)
        self.notification_panel.reportRequested.connect(lambda: self._go("Attendance Report"))
        self.notification_panel.closed.connect(self._notifications_closed)

        self.user_chip = UserChip(theme=theme)
        self.user_chip.clicked.connect(self._toggle_user_menu)
        account.addWidget(self.user_chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self.avatar = self.user_chip.avatar
        self.user_menu = UserMenuPanel(theme, self)
        self.user_menu.profileRequested.connect(self._show_profile)
        self.user_menu.usersRequested.connect(lambda: self._go("Users & Access"))
        self.user_menu.settingsRequested.connect(lambda: self._go("Settings"))
        self.user_menu.signOutRequested.connect(self._logout)
        self.user_menu.closed.connect(lambda: self.user_chip.set_open(False))
        row.addLayout(account)
        self._sync_identity()
        return header

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)

        column = QVBoxLayout(sidebar)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        # ── navigation ──────────────────────────────────────────────────
        section = QLabel("MAIN MENU")
        section.setObjectName("brandMark")
        section.setContentsMargins(28, 20, 18, 6)
        column.addWidget(section)
        self.nav_section = section

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

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_relaunch = QPushButton("  Relaunch")
        self.btn_relaunch.setObjectName("controlButton")
        self.btn_relaunch.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_button_icon(self.btn_relaunch, "restart", 14)
        self.btn_relaunch.setToolTip("Restart the application")
        self.btn_relaunch.clicked.connect(lambda: self._relaunch_app())
        self.btn_quit = QPushButton("  Quit")
        self.btn_quit.setObjectName("controlButtonDanger")
        self.btn_quit.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_button_icon(self.btn_quit, "power", 14)
        self.btn_quit.setToolTip("Shut down the application")
        self.btn_quit.clicked.connect(lambda: self._quit_app())
        btn_row.addWidget(self.btn_relaunch)
        btn_row.addWidget(self.btn_quit)
        footer_layout.addLayout(btn_row)

        version = QLabel(f"{APP_VERSION}\n\u00a9 2026 SV Trucking Face Recognition. All rights reserved.")
        version.setObjectName("versionLabel")
        version.setContentsMargins(4, 0, 0, 0)
        footer_layout.addWidget(version)
        tagline = QLabel("Secure \u2022 Accurate \u2022 Smarter")
        tagline.setObjectName("legalLabel")
        tagline.setContentsMargins(4, 0, 0, 0)
        footer_layout.addWidget(tagline)
        column.addWidget(footer)
        return sidebar

    def _build_status_bar(self):
        bar = QStatusBar()
        self.setStatusBar(bar)
        bar.showMessage(
            "Follow the selected attendance requirement in Live Monitor.  "
            "Keep your whole face visible.")

        usage_widget = QWidget()
        usage_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        usage_widget.setToolTip("Click to open Live Usage")
        usage_widget.mousePressEvent = lambda _: self._go("Live Usage")
        row = QHBoxLayout(usage_widget)
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(14)

        self._sb_ram = QLabel("RAM —")
        self._sb_cpu = QLabel("CPU —")
        self._sb_disk = QLabel("Disk —")
        for lbl in (self._sb_ram, self._sb_cpu, self._sb_disk):
            row.addWidget(lbl)
        self._style_status_labels()

        bar.addPermanentWidget(usage_widget)

        self._sys_monitor = SysMonitor(interval=2.0)
        self._sys_monitor.snapshotReady.connect(self._on_sys_snapshot)
        self._sys_monitor.start()

    def _style_status_labels(self):
        c = palette(self.settings.theme)
        style = f"font-size: 11px; font-weight: 600; color: {c['text_secondary']};"
        for lbl in (self._sb_ram, self._sb_cpu, self._sb_disk):
            lbl.setStyleSheet(style)

    def _on_sys_snapshot(self, snap):
        ram_gb = snap.ram_used_bytes / (1024 ** 3)
        self._sb_ram.setText(f"RAM {ram_gb:.2f} GB")
        self._sb_cpu.setText(f"CPU {snap.cpu_percent:.1f}%")
        disk_gb_used = snap.disk_used_bytes / (1024 ** 3)
        disk_gb_total = snap.disk_total_bytes / (1024 ** 3)
        self._sb_disk.setText(f"Disk: {disk_gb_used:.1f} GB used (limit {disk_gb_total:.1f} GB)")

    def _connect(self):
        self.engine.runningChanged.connect(self._engine_state)
        self.engine.pausedChanged.connect(lambda _: self._engine_state(self.engine.running))
        self.engine.captureTriggered.connect(lambda _: self.capture_flash.trigger())
        self.engine.runningChanged.connect(lambda running: None if running else self.capture_flash.cancel())
        self.engine.attendanceFailed.connect(lambda _: self.capture_flash.cancel())
        live = self.screens[0]
        live.flashPreviewRequested.connect(lambda: self.capture_flash.trigger(preview=True))
        live.viewAllRequested.connect(lambda: self._go("Attendance Report"))
        live.settingsRequested.connect(lambda: self._go("Settings"))
        live.settingsApplied.connect(self._broadcast_settings)
        ctx = self.screens[5]
        ctx.settingsApplied.connect(
            lambda settings, restart: self._broadcast_settings(settings, restart))
        self.screens[4].usersChanged.connect(self._on_users_changed)
        updates_page = self.screens[5].updates_page
        updates_page.installRequested.connect(self._install_update)
        updates_page.updateFound.connect(self._on_update_found)
        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._tick)
        self.clock_timer.start()
        self._tick()

    # --- state propagation ------------------------------------------------
    def _engine_state(self, running):
        paused = running and self.engine.paused
        state = "PAUSED" if paused else "RUNNING" if running else "STOPPED"
        tone = "warn" if paused else "ok" if running else "idle"
        self.engine_pill.set_status(f"ENGINE {state}", tone)
        colors = palette(self.settings.theme)
        if running:
            self.sys_status_label.setText("Recording Paused" if paused else "All Systems Operational")
            self.sys_status_dot.set_icon_color(colors["warn" if paused else "success"])
        else:
            self.sys_status_label.setText("Engine Stopped")
            self.sys_status_dot.set_icon_color(colors["muted"])
        self._refresh_header()

    def _refresh_header(self, *_):
        try:
            enrolled = self.engine.catalog.enrolled_count
        except Exception:
            enrolled = 0
        if self.stack.currentWidget() is self.no_access_page:
            self.page_title.setText("No access")
        else:
            self.page_title.setText(NAV[max(0, self.nav.currentRow())][1])
        details = (f"{enrolled} {'employee' if enrolled == 1 else 'employees'} enrolled"
                   f"  \u2022  {describe_source(self.settings.source)}")
        self.header_details.setText(details)

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
        theme = self.settings.theme
        self.engine_pill.set_theme(theme)
        self.no_access_tile.set_tone("orange", theme)
        apply_button_icon(self.no_access_sign_out, "log-out", palette(theme)["danger"], 14)
        self.notif_button.set_theme(theme)
        self.user_chip.set_theme(theme)
        self.notification_panel.set_theme(theme)
        self.user_menu.set_theme(theme)
        self._refresh_nav_icons()
        self._style_status_labels()
        self._engine_state(self.engine.running)

    def _go(self, title):
        """Switch to the navigation entry labelled *title* (refused without access)."""
        for row, (_icon, label) in enumerate(NAV):
            if label == title:
                self.nav.setCurrentRow(row)
                return

    # --- role-based access ----------------------------------------------------
    def _build_no_access_page(self):
        page = QWidget()
        page.setObjectName("plain")
        column = QVBoxLayout(page)
        column.setContentsMargins(40, 40, 40, 40)
        column.setSpacing(0)
        column.addStretch(1)
        self.no_access_tile = IconTile("lock", tone="orange", size=72, theme=self.settings.theme)
        column.addWidget(self.no_access_tile, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(18)
        title = QLabel("No areas available")
        title.setObjectName("dialogTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        column.addSpacing(6)
        body = QLabel("Your account is signed in but has no permissions yet.\n"
                      "Ask an administrator to grant access in Users & Access.")
        body.setObjectName("dialogSubtitle")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(body)
        column.addSpacing(8)
        self.no_access_account = QLabel("")
        self.no_access_account.setObjectName("fieldHelp")
        self.no_access_account.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.no_access_account)
        column.addSpacing(20)
        self.no_access_sign_out = QPushButton("  Sign out")
        self.no_access_sign_out.setObjectName("controlButton")
        self.no_access_sign_out.setCursor(Qt.CursorShape.PointingHandCursor)
        self.no_access_sign_out.setMinimumHeight(36)
        self.no_access_sign_out.clicked.connect(self._logout)
        column.addWidget(self.no_access_sign_out, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addStretch(1)
        return page

    def _on_nav_changed(self, row):
        if row < 0:
            return
        title = NAV[row][1]
        if not self.access.allows_nav(title):
            # Hidden entries can still be reached programmatically: refuse and explain.
            self.nav.blockSignals(True)
            self.nav.setCurrentRow(self._last_row)
            self.nav.blockSignals(False)
            self._refresh_nav_icons()
            self.access.deny(NAV_PERMISSIONS[title], f"open {title}", self)
            return
        self._last_row = row
        self.stack.setCurrentIndex(row)
        self._refresh_header()
        self._refresh_nav_icons()

    def _apply_access(self):
        """Show only what the signed-in account may open; move off a screen it lost."""
        access = self.access
        allowed = [row for row, (_icon, title) in enumerate(NAV) if access.allows_nav(title)]
        for row in range(self.nav.count()):
            self.nav.item(row).setHidden(row not in allowed)
        self.nav_section.setVisible(bool(allowed))
        self.no_access_account.setText(access.account_label())
        self.user_menu.set_shortcuts(users=access.allows_nav("Users & Access"),
                                     settings=access.allows_nav("Settings"))
        live_events = access.allows("view_dashboard")
        if not live_events:
            self.notification_panel.close()
        self.notif_button.setVisible(live_events)
        self.notification_panel.set_report_link_visible(access.allows_nav("Attendance Report"))
        for screen in self.screens:
            hook = getattr(screen, "apply_access", None)
            if callable(hook):
                hook(access)
        if not allowed:
            self._last_row = -1
            self.nav.blockSignals(True)
            self.nav.setCurrentRow(-1)
            self.nav.blockSignals(False)
            self.stack.setCurrentWidget(self.no_access_page)
            self._refresh_header()
            self._refresh_nav_icons()
        elif self.nav.currentRow() not in allowed:
            self._last_row = allowed[0]
            self.nav.setCurrentRow(allowed[0])
        elif self.stack.currentWidget() is self.no_access_page:
            self._on_nav_changed(self.nav.currentRow())

    def _apply_account(self, fresh):
        """The signed-in account changed elsewhere: sign out, or update access in place."""
        if self.current_user is None:
            return
        if fresh is None or fresh.disabled:
            self._force_sign_out("Your account was removed." if fresh is None
                                 else "Your account was disabled by an administrator.")
            return
        access_changed = ((fresh.role, sorted(fresh.permissions))
                          != (self.current_user.role, sorted(self.current_user.permissions)))
        self.current_user = fresh
        if self.account_watcher is not None:
            self.account_watcher.set_user(fresh)
        self.screens[4].set_current_user(fresh)
        self._sync_identity()
        self.access.set_user(fresh)
        if access_changed:
            self._note("Your access was updated by an administrator.")

    def _force_sign_out(self, reason):
        if getattr(self, "_signing_out", False):
            return
        self._signing_out = True
        if self.account_watcher is not None:
            self.account_watcher.stop()
        from ..users import clear_session
        clear_session()
        box = QMessageBox(QMessageBox.Icon.Information, "Signed out", reason,
                          QMessageBox.StandardButton.Ok, self)
        box.setInformativeText("The dashboard will restart at the sign-in screen.")
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.finished.connect(lambda _result: self._relaunch_app(mode="signout"))
        box.open()

    # --- signed-in identity -------------------------------------------------
    def _identity(self):
        """(name, role subtitle, avatar path, menu meta line, role) for the header."""
        user = self.current_user
        if user is not None:
            return (user.full_name or user.username, role_title(user.role), user.avatar_path,
                    user.email or f"@{user.username}", role_title(user.role))
        mode = "PIN access" if self.settings.dashboard_pin_hash else "Local access"
        return "Administrator", mode, None, "Signed in on this computer", ""

    def _sync_identity(self):
        name, subtitle, image, meta, role = self._identity()
        self.user_chip.set_user(name, subtitle, image)
        self.user_menu.set_user(name, meta, role, image,
                                can_sign_out=bool(self.current_user or self.settings.dashboard_pin_hash),
                                has_profile=self.current_user is not None)

    def _on_users_changed(self, users):
        if self.current_user is None:
            return
        fresh = next((user for user in users if user.uuid == self.current_user.uuid), None)
        self._apply_account(fresh)

    def _toggle_user_menu(self):
        menu = self.user_menu
        if menu.isVisible():
            menu.close()
            return
        if menu.recently_closed():
            return
        self._sync_identity()
        self.user_chip.set_open(True)
        menu.open_below(self.user_chip)

    def _show_profile(self):
        if not self.current_user:
            self._note("No user session active.")
            return
        try:
            from ..users import UserRepository
            repo = UserRepository(self.settings)
            fresh = repo.get_user(self.current_user.uuid)
            if fresh:
                self.current_user = fresh
        except Exception:
            pass
        dlg = ProfileDialog(self.current_user, self.settings,
                            theme=self.settings.theme, parent=self)
        dlg.exec()
        self._sync_identity()

    def _logout(self):
        from ..users import clear_session
        clear_session()
        self._relaunch_app(mode="signout")

    # --- notifications ------------------------------------------------------
    def _toggle_notifications(self):
        panel = self.notification_panel
        if panel.isVisible():
            panel.close()
            return
        if panel.recently_closed():
            return
        self.notifications.mark_seen()
        self.notif_button.set_open(True)
        panel.open_below(self.notif_button)

    def _notifications_closed(self):
        self.notif_button.set_open(False)
        # Everything shown in the panel has now been read.
        self.notifications.mark_read()

    def _on_notifications_changed(self):
        if self.notification_panel.isVisible():
            self.notifications.mark_seen()
        self.notif_button.set_count(self.notifications.unseen_count())

    @staticmethod
    def _face_thumbnail(frame):
        pixmap = to_pixmap(frame)
        if pixmap.isNull():
            return None
        return pixmap.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                             Qt.TransformationMode.SmoothTransformation)

    @staticmethod
    def _evidence(result, status, detail):
        """What the capture-detail dialog needs, minus the (large) frames themselves."""
        job = result.job
        return {"name": job.employee_name, "employee_id": job.employee_id, "status": status,
                "detail": detail, "snapshot": result.snapshot or "", "face_box": job.face_box,
                "captured_at": datetime.fromtimestamp(job.captured_at).strftime("%d %b %Y · %H:%M:%S"),
                "duration": f"{job.duration:.1f}s", "event_id": job.event_id}

    def _notify_saved(self, result):
        job = result.job
        self.notifications.add(
            "checkin", job.employee_name,
            f"Checked in • ID {job.employee_id} • {job.duration:.1f}s verified",
            thumbnail=self._face_thumbnail(job.frame),
            payload=self._evidence(result, "Check-in saved", f"{job.duration:.1f}s verified presence"))

    def _notify_failed(self, result):
        job = result.job
        error = str(result.error or "Unknown error")
        self.notifications.add(
            "failed", "Attendance not saved", f"{job.employee_name} • {error}",
            thumbnail=self._face_thumbnail(job.frame),
            payload=self._evidence(result, "Save failed", error))

    def _notify_unknown(self, info):
        seconds = float(info.get("age") or 0)
        self.notifications.add(
            "unknown", "Unknown face detected",
            f"Unrecognized person in view for {seconds:.0f}s • track #{info.get('track_id')}")

    def _notify_error(self, message):
        self.notifications.add("error", "Engine error", str(message))

    def _open_notification(self, item):
        payload = dict(item.payload or {})
        if item.kind == "update":
            self._open_updates()
            return
        if item.kind not in ("checkin", "failed") or not payload:
            self._go("Live Monitor")
            return
        snapshot = payload.pop("snapshot", "")
        capture = load_photo(snapshot)
        if capture.isNull() and item.thumbnail is not None:
            capture = item.thumbnail
        context = context_path(snapshot) if snapshot else None
        payload.update(capture=capture,
                       context_path=str(context) if context is not None and context.is_file() else "",
                       context_capture=None, time=item.created.strftime("%H:%M:%S"))
        self.screens[0]._open_activity_detail(payload)

    def _broadcast_settings(self, settings, restart):
        self.settings = settings
        if self.account_watcher is not None:
            self.account_watcher.settings = settings
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
        self.access.theme = theme
        application = QApplication.instance()
        if application is not None:
            application.setStyleSheet(stylesheet(theme))
        for screen in getattr(self, "screens", []):
            setter = getattr(screen, "set_theme", None)
            if callable(setter):
                setter(theme)
        if hasattr(self, "user_chip"):
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
    def on_unhandled_error(self, details):
        """Leave the window available for diagnosis; pause new attendance."""
        try:
            self.capture_flash.cancel()
            self.engine.set_paused(True)
            self.screens[0]._update_pause_button()
            self.statusBar().showMessage("Dashboard error — attendance paused. See error details.")
            message = QMessageBox(QMessageBox.Icon.Warning, "Dashboard error",
                                  "Attendance has been paused after an application error.\n"
                                  "The window will remain open. Restart the dashboard before resuming.",
                                  QMessageBox.StandardButton.Ok, self)
            message.setDetailedText(details)
            message.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            message.open()
        except Exception:
            logging.exception("Could not display dashboard error")

    def _quit_app(self):
        self._begin_exit("quit")

    def _relaunch_app(self, mode="restart"):
        self._begin_exit(mode)

    def _begin_exit(self, mode, detail=None):
        """Stop everything behind a progress card, then quit, restart or update (see shutdown.py)."""
        if self._exit is not None or self._exit_done:
            return
        if self.account_watcher is not None:
            self.account_watcher.stop()
        self.notification_panel.close()
        self.user_menu.close()
        self.capture_flash.cancel()
        self._note({"quit": "Shutting down\u2026", "signout": "Signing out\u2026",
                    "update": "Installing update\u2026"}.get(mode, "Restarting\u2026"))
        self._exit = ShutdownSequence(self, mode, services=(self.telegram, self._sys_monitor),
                                      detail=detail)
        self._exit.finished.connect(self._complete_exit)
        self._exit.start()

    def _complete_exit(self, mode):
        self._exit_done = True
        if mode == "update":
            self._launch_installer()
        elif mode != "quit":
            # Only now: the engine lock is released, so the new instance can take it.
            self._spawn_replacement()
        self._exit.overlay.done(0)
        self.close()

    # --- application updates ------------------------------------------------
    def _auto_check_updates(self):
        if self._exit is not None or not self.access.allows("manage_settings"):
            return
        self._prompt_for_update = True
        self.screens[5].updates_page.check_now()

    def _on_update_found(self, release):
        if self._notified_version != release.version:
            self._notified_version = release.version
            self.notifications.add("update", f"Version {release.version} is available",
                                   "Open Settings \u203a Updates to install it.")
        if not self._prompt_for_update or self._exit is not None:
            return
        self._prompt_for_update = False
        prompt = UpdatePromptDialog(release, self.settings.theme, self)
        prompt.finished.connect(self._update_prompt_done)
        self._update_prompt = prompt
        prompt.open()

    def _update_prompt_done(self, _result):
        prompt, self._update_prompt = self._update_prompt, None
        if prompt is None:
            return
        page = self.screens[5].updates_page
        if prompt.choice == prompt.INSTALL:
            self._open_updates()
            page.install_update()
        elif prompt.choice == prompt.SKIP and page.current_release() is not None:
            page.skip_version(page.current_release())
        prompt.deleteLater()

    def _open_updates(self):
        self._go("Settings")
        if self.stack.currentWidget() is self.screens[5]:
            self.screens[5].show_updates()

    def _install_update(self, release, package):
        if not guard(self, "manage_settings", "install updates"):
            return
        try:
            self._pending_install = updates.install_command(
                package, updates.installed_location(), os.getpid())
        except updates.UpdateError as exc:
            QMessageBox.warning(self, "Can't install the update", str(exc))
            return
        self._begin_exit("update", detail=f"Version {release.version} will be installed, "
                                          "then SV Face ID reopens by itself.")

    def _launch_installer(self):
        from PyQt6.QtCore import QProcess
        program, arguments = self._pending_install
        started, _pid = QProcess.startDetached(program, arguments)
        if not started:
            logging.error("Could not start the update installer: %s %s", program, arguments)
            self._spawn_replacement()      # at least come back on the current version

    def _spawn_replacement(self):
        from PyQt6.QtCore import QProcess
        program, arguments = relaunch_command()
        started, _pid = QProcess.startDetached(program, arguments)
        if not started:
            logging.error("Could not restart the dashboard: %s %s", program, arguments)

    def closeEvent(self, event):
        if not self._exit_done:
            if self._exit is not None:
                event.ignore()       # already stopping; the sequence closes the window
                return
            if event.spontaneous():
                # The title-bar close button: stop gracefully behind the progress card.
                event.ignore()
                self._begin_exit("quit")
                return
        if self.account_watcher is not None:
            self.account_watcher.stop()
        self.notification_panel.close()
        self.user_menu.close()
        self.capture_flash.cancel()
        self.clock_timer.stop()
        if not self._exit_done:
            # Programmatic close (tests, error paths): stop synchronously.
            self._sys_monitor.stop()
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
    errors = DashboardErrorHandler(ROOT / "logs" / "dashboard-errors.log", application)
    errors.install()
    try:
        settings = Settings.load(settings_path)

        def _show_main():
            from ..users import (UserRepository, accounts_expected, clear_session, load_session,
                                 remember_accounts_exist, save_session)
            application.setStyleSheet(stylesheet(settings.theme))
            current_user = None
            repo = UserRepository(settings)
            try:
                has_users = repo.has_users()
            except Exception:
                logging.exception("Could not check user accounts")
                # An unreachable database must not switch sign-in off on a machine
                # that has had accounts; the sign-in dialog reports the problem.
                has_users = accounts_expected()
            else:
                if has_users:
                    remember_accounts_exist()

            if has_users:
                notice = ""
                session = load_session()
                if session:
                    try:
                        user = repo.get_user(session.user_uuid)
                    except Exception:
                        logging.exception("Could not restore the remembered session")
                    else:
                        if user is not None and not user.disabled:
                            current_user = user
                        else:
                            clear_session()
                            if user is not None:
                                notice = "Your account has been disabled. Ask an administrator to enable it again."
                if not current_user:
                    dialog = UserLoginDialog(settings, theme=settings.theme, version=APP_VERSION,
                                             notice=notice)
                    if dialog.exec() != QDialog.DialogCode.Accepted:
                        application.quit()
                        return
                    current_user = dialog.current_user
                    if dialog.remember and current_user:
                        save_session(current_user)
            elif settings.dashboard_pin_hash:
                dialog = LoginDialog(settings.dashboard_pin_hash, theme=settings.theme,
                                     version=APP_VERSION)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    application.quit()
                    return

            window = MainWindow(settings, autostart=autostart,
                                current_user=current_user)
            errors.errorRaised.connect(window.on_unhandled_error)
            window.show()
            _show_main.window = window

        application.setQuitOnLastWindowClosed(False)
        splash = SplashScreen(settings=settings)
        _show_main.window = None

        def _on_splash_done():
            _show_main()
            application.setQuitOnLastWindowClosed(True)

        splash.start(on_finished=_on_splash_done)
        result = application.exec()
        # Quitting mid-splash must not let a late animation open the sign-in flow.
        splash.cancel()
        return result
    finally:
        errors.close()


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
