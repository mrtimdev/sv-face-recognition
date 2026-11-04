"""First-run setup wizard: welcome, database, MySQL, administrator, done."""
from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                             QStackedWidget, QVBoxLayout, QWidget)

from .. import first_run
from ..hrm_sync import HRMConfig, HRMError
from ..hrm_sync import _base_url as hrm_base_url
from ..users import DEFAULT_ADMIN_USERNAME, UserRepository
from .login import AuthHero
from .threads import start_detached
from .widgets import StatusPill
from .widgets.form import ChoiceCard, FormField, TextInput

STEPS = ("Welcome", "Database", "MySQL", "Administrator", "Finish")
WELCOME, DATABASE, MYSQL, ADMIN, FINISH = range(5)


class _ProvisionWorker(QThread):
    step = pyqtSignal(str)
    done = pyqtSignal(object, object)  # (values, has_accounts) or (None, error text)

    def __init__(self, settings, admin_user, admin_password, port, database):
        super().__init__()
        self._arguments = (settings, admin_user, admin_password, port, database)

    def run(self):
        try:
            result = first_run.provision_local_mysql(*self._arguments, on_step=self.step.emit)
        except first_run.SetupError as exc:
            self.done.emit(None, str(exc))
        except Exception as exc:
            self.done.emit(None, str(exc) or type(exc).__name__)
        else:
            self.done.emit(*result)


class _StepTrail(QWidget):
    """Shows where the person is; MySQL only appears when it was chosen."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(14)
        self._labels = {}
        for index, name in enumerate(STEPS):
            label = QLabel(name)
            label.setObjectName("authStatus")
            self._labels[index] = label
            self._row.addWidget(label)
        self._row.addStretch(1)

    def show_step(self, current, include_mysql):
        self._labels[MYSQL].setVisible(include_mysql)
        for index, label in self._labels.items():
            prefix = "✓ " if index < current else ""
            label.setText(prefix + STEPS[index])
            font = label.font()
            font.setBold(index == current)
            label.setFont(font)


class SetupWizard(QDialog):
    """Settings are saved at the last step, so closing early means setup runs again."""

    def __init__(self, settings, theme="light", version="", existing=False, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._existing = existing
        self._theme = theme
        self._values = {}
        self._has_accounts = False
        self._worker = None
        self.setWindowTitle("SV Face ID Setup")
        self.setFixedSize(860, 600)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.hero = AuthHero(version)
        root.addWidget(self.hero)
        panel = QFrame()
        panel.setObjectName("authForm")
        column = QVBoxLayout(panel)
        column.setContentsMargins(40, 28, 40, 24)
        column.setSpacing(14)
        self.trail = _StepTrail()
        column.addWidget(self.trail)
        self.pages = QStackedWidget()
        self.pages.setStyleSheet("QStackedWidget, QWidget#plain { background: transparent; }")
        for build in (self._build_welcome, self._build_database, self._build_mysql,
                      self._build_admin, self._build_finish):
            self.pages.addWidget(build())
        column.addWidget(self.pages, 1)
        self.error = QLabel("")
        self.error.setObjectName("authStatus")
        self.error.setProperty("tone", "bad")
        self.error.setWordWrap(True)
        column.addWidget(self.error)
        buttons = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("Get started")
        self.next_button.setObjectName("primary")
        self.next_button.setMinimumHeight(42)
        self.next_button.setDefault(True)
        self.back_button.setMinimumHeight(42)
        self.back_button.setAutoDefault(False)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        buttons.addWidget(self.next_button)
        column.addLayout(buttons)
        root.addWidget(panel, 1)
        self.back_button.clicked.connect(self._back)
        self.next_button.clicked.connect(self._next)
        self._go(WELCOME)

    # --- pages ---------------------------------------------------------------

    @staticmethod
    def _page(eyebrow, title, subtitle=""):
        page = QWidget()
        page.setObjectName("plain")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        eyebrow_label = QLabel(eyebrow.upper())
        eyebrow_label.setObjectName("eyebrow")
        layout.addWidget(eyebrow_label)
        title_label = QLabel(title)
        title_label.setObjectName("authTitle")
        layout.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("authSubtitle")
            subtitle_label.setWordWrap(True)
            layout.addWidget(subtitle_label)
        layout.addSpacing(10)
        return page, layout

    def _build_welcome(self):
        page, layout = self._page("Setup", "Welcome to SV Face ID",
                                  "Review how this computer stores its data." if self._existing else
                                  "A few quick steps and this computer is ready to record attendance.")
        for text in (("Keep your current database or move to MySQL on this computer",
                      "Your existing accounts and attendance are left untouched",
                      "Close this window to skip; it won't appear again") if self._existing else
                     ("Choose where attendance and accounts are stored",
                      "Connect SV HRM, whose accounts sign in to this dashboard",
                      "Close this window at any time and setup starts again next time")):
            item = QLabel("✓  " + text)
            item.setObjectName("authSubtitle")
            layout.addWidget(item)
        layout.addStretch(1)
        return page

    def _build_database(self):
        page, layout = self._page("Database", "Where should data be stored?",
                                  "You can change this later in Settings.")
        self.sqlite_card = ChoiceCard("database", "Keep the current database" if self._existing else "Built-in database",
                                      "Nothing changes." if self._existing else
                                      "Nothing else to install. Best for one computer.",
                                      tone="green", theme=self._theme)
        self.mysql_card = ChoiceCard("database", "MySQL on this computer",
                                     "Starts a new MySQL database. Existing accounts and attendance "
                                     "are not moved into it." if self._existing else
                                     "Uses a MySQL server installed on this computer.",
                                     tone="blue", theme=self._theme)
        self.sqlite_card.setChecked(True)
        self.sqlite_card.clicked.connect(lambda: self._choose_mysql(False))
        self.mysql_card.clicked.connect(lambda: self._choose_mysql(True))
        layout.addWidget(self.sqlite_card)
        layout.addWidget(self.mysql_card)
        layout.addStretch(1)
        return page

    def _build_mysql(self):
        page, layout = self._page("MySQL", "Connect to MySQL",
                                  "Used once to create the database and a private user. "
                                  "This password is not saved.")
        status_row = QHBoxLayout()
        self.mysql_status = StatusPill("Checking…", "idle", dot=True, theme=self._theme)
        self.check_button = QPushButton("Check again")
        self.check_button.setObjectName("softButton")
        self.check_button.setAutoDefault(False)
        self.check_button.clicked.connect(self._check_mysql)
        status_row.addWidget(self.mysql_status)
        status_row.addStretch(1)
        status_row.addWidget(self.check_button)
        layout.addLayout(status_row)

        self.install_help = QWidget()
        self.install_help.setObjectName("plain")
        help_row = QHBoxLayout(self.install_help)
        help_row.setContentsMargins(0, 0, 0, 0)
        self.download_button = QPushButton("Download MySQL")
        self.copy_button = QPushButton("Copy install command")
        for button in (self.download_button, self.copy_button):
            button.setObjectName("softButton")
            button.setAutoDefault(False)
            help_row.addWidget(button)
        help_row.addStretch(1)
        self.download_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(first_run.mysql_download_url())))
        self.copy_button.clicked.connect(self._copy_install_command)
        layout.addWidget(self.install_help)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.mysql_user = TextInput("root", icon="user", theme=self._theme)
        self.mysql_user.setText("root")
        self.mysql_password = TextInput("MySQL administrator password", password=True, theme=self._theme)
        self.mysql_port = TextInput("3306", theme=self._theme)
        self.mysql_port.setText(str(first_run.MYSQL_PORT))
        self.mysql_database = TextInput(first_run.DEFAULT_DATABASE, icon="database", theme=self._theme)
        self.mysql_database.setText(first_run.DEFAULT_DATABASE)
        grid.addWidget(FormField("Administrator", self.mysql_user, theme=self._theme), 0, 0)
        grid.addWidget(FormField("Password", self.mysql_password, theme=self._theme), 0, 1)
        grid.addWidget(FormField("Port", self.mysql_port, theme=self._theme), 1, 0)
        grid.addWidget(FormField("Database name", self.mysql_database, theme=self._theme), 1, 1)
        layout.addLayout(grid)
        self.mysql_progress = QLabel("")
        self.mysql_progress.setObjectName("authStatus")
        layout.addWidget(self.mysql_progress)
        layout.addStretch(1)
        return page

    def _build_admin(self):
        page, layout = self._page("Administrator", "Create the administrator",
                                  "This local account is for the mobile API.")
        self.admin_username = TextInput("Username", icon="user", theme=self._theme)
        self.admin_username.setText(DEFAULT_ADMIN_USERNAME)
        self.admin_password = TextInput("Password", password=True, theme=self._theme)
        self.admin_confirm = TextInput("Repeat password", password=True, theme=self._theme)
        self.admin_fields = QWidget()
        self.admin_fields.setObjectName("plain")
        fields = QVBoxLayout(self.admin_fields)
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(10)
        fields.addWidget(FormField("Username", self.admin_username, theme=self._theme))
        fields.addWidget(FormField("Password", self.admin_password, optional=True, theme=self._theme,
                                   help_text="Leave empty to use the default release password."))
        fields.addWidget(FormField("Repeat password", self.admin_confirm, optional=True, theme=self._theme))
        layout.addWidget(self.admin_fields)
        self.accounts_note = QLabel("This database already has local accounts for the mobile API; "
                                    "no new account is needed.")
        self.accounts_note.setObjectName("authSubtitle")
        self.accounts_note.setWordWrap(True)
        layout.addWidget(self.accounts_note)
        self.hrm_address = TextInput("https://hrm.example.com", theme=self._theme)
        self.hrm_address.setText(getattr(self.settings, "hrm_api_url", "") or "")
        layout.addWidget(FormField("SV HRM address", self.hrm_address, optional=True, theme=self._theme,
                                   help_text="Dashboard sign-in uses SV HRM accounts. Leave empty for "
                                             "SV HRM on this computer's network address."))
        layout.addStretch(1)
        return page

    def _build_finish(self):
        page, layout = self._page("Finish", "You're all set", "SV Face ID is ready to use.")
        self.summary = QLabel("")
        self.summary.setObjectName("authSubtitle")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addStretch(1)
        return page

    # --- navigation ----------------------------------------------------------

    def _uses_mysql(self):
        return self.mysql_card.isChecked()

    def _choose_mysql(self, use_mysql):
        self.mysql_card.setChecked(use_mysql)
        self.sqlite_card.setChecked(not use_mysql)
        self._values, self._has_accounts = {}, False
        self.trail.show_step(DATABASE, use_mysql)

    def _go(self, index):
        self.error.setText("")
        self.pages.setCurrentIndex(index)
        self.trail.show_step(index, self._uses_mysql())
        self.back_button.setVisible(WELCOME < index < FINISH)
        self.next_button.setText({WELCOME: "Get started", MYSQL: "Create database",
                                  ADMIN: "Finish setup" if not self._has_accounts else "Continue",
                                  FINISH: "Open SV Face ID"}.get(index, "Continue"))
        if index == MYSQL:
            self._check_mysql()
        elif index == ADMIN:
            self.admin_fields.setVisible(not self._has_accounts)
            self.accounts_note.setVisible(self._has_accounts)

    def _back(self):
        index = self.pages.currentIndex()
        self._go(DATABASE if index == ADMIN and not self._uses_mysql() else index - 1)

    def _next(self):
        index = self.pages.currentIndex()
        if index == WELCOME:
            self._go(DATABASE)
        elif index == DATABASE:
            if self._uses_mysql():
                self._go(MYSQL)
                return
            try:
                self._has_accounts = UserRepository(self.settings).has_users()
            except Exception as exc:
                self.error.setText(str(exc))
                return
            self._go(ADMIN)
        elif index == MYSQL:
            self._provision()
        elif index == ADMIN:
            self._finish_setup()
        else:
            self.accept()

    # --- MySQL ---------------------------------------------------------------

    def _port(self):
        try:
            port = int(self.mysql_port.text().strip())
        except ValueError:
            return None
        return port if 1 <= port <= 65535 else None

    def _check_mysql(self):
        port = self._port()
        running = port is not None and first_run.mysql_running(port)
        self.mysql_status.set_status("MySQL is running" if running else "MySQL not found on this computer",
                                     "ok" if running else "warn")
        self.install_help.setVisible(not running)
        return running

    def _copy_install_command(self):
        QGuiApplication.clipboard().setText(first_run.mysql_install_command())
        self.mysql_progress.setText("Install command copied. Run it in a terminal, then Check again.")

    def _provision(self):
        port = self._port()
        if port is None:
            self.error.setText("Enter a port between 1 and 65535.")
            return
        self.error.setText("")
        self._set_busy(True)
        self._worker = _ProvisionWorker(self.settings, self.mysql_user.text(), self.mysql_password.text(),
                                        port, self.mysql_database.text())
        self._worker.step.connect(lambda text: self.mysql_progress.setText(text + "…"))
        self._worker.done.connect(self._on_provisioned)
        start_detached(self._worker)

    def _on_provisioned(self, values, detail):
        self._set_busy(False)
        self.mysql_progress.setText("")
        if values is None:
            self.error.setText(str(detail))
            return
        self.mysql_password.clear()
        self._values, self._has_accounts = values, bool(detail)
        self._go(ADMIN)

    def _set_busy(self, busy):
        self.next_button.setEnabled(not busy)
        self.back_button.setEnabled(not busy)
        self.hero.set_mode("busy" if busy else "idle")

    def reject(self):
        if self.next_button.isEnabled():
            super().reject()

    # --- finishing -----------------------------------------------------------

    def _finish_setup(self):
        password = self.admin_password.text()
        if not self._has_accounts and password != self.admin_confirm.text():
            self.error.setText("The two passwords don't match.")
            return
        address = self.hrm_address.text().strip()
        try:
            if address:
                hrm_base_url(HRMConfig(base_url=address))
        except HRMError as exc:
            self.error.setText(str(exc))
            return
        self.settings.update({**self._values, "hrm_api_url": address})
        try:
            if not self._has_accounts:
                first_run.create_administrator(self.settings, self.admin_username.text(), password)
            self.settings.save()
        except Exception as exc:
            self.error.setText(str(exc))
            return
        if self._uses_mysql():
            location = f"Data is stored in the MySQL database “{self.settings.db_name}” on this computer."
        else:
            location = "Your database is unchanged." if self._existing else "Data is stored in the built-in database."
        account = ("your existing accounts" if self._has_accounts
                   else f"the account “{self.admin_username.text().strip()}”")
        self.summary.setText(f"{location} The mobile API signs in with {account}. "
                             "The dashboard signs in with your SV HRM account.")
        self.admin_password.clear()
        self.admin_confirm.clear()
        self._go(FINISH)
