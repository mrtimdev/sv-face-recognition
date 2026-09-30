"""Users & Access: manage accounts, roles, permissions and the signed-in profile."""
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QImageReader, QLinearGradient, QPainter, QRadialGradient
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QDialog,
                             QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                             QLabel, QMessageBox, QPushButton, QScrollArea, QSizePolicy,
                             QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from ...config import ROOT
from ...users import ALL_PERMISSIONS, ROLE_DEFAULTS, UserRepository, verify_password
from ..access import PERMISSION_INFO as PERMISSIONS
from ..icons import IconLabel, apply_button_icon, make_icon
from ..theme import palette
from ..threads import settle
from ..widgets import Avatar, Card, EmptyState, IconTile, PageHeader, StatCard, ToastBar
from ..widgets.avatar import load_avatar
from ..widgets.form import (EMAIL_PATTERN, ChoiceCard, FormField, PermissionTile,
                            SectionHeading, StrengthMeter, TextInput, repolish)
from ..widgets.loading import ProgressLine
from ..widgets.user_menu import role_title

AVATAR_DIR = ROOT / "avatars"
AVATAR_SIZE = 256

# role -> (glyph, title, description, tile tone)
ROLE_CHOICES = {
    "admin": ("shield", "Administrator", "Full access, including users and settings.", "orange"),
    "user": ("user", "Standard user", "Day-to-day access to monitoring and reports.", "blue"),
}

ROLE_TIPS = {
    "admin": "Administrators can manage every area of the dashboard, including other accounts.",
    "user": "Standard users start with the live monitor and reports. Grant more below if needed.",
}

FILTERS = (("all", "All"), ("admin", "Admins"), ("user", "Users"), ("disabled", "Disabled"))
FILTER_SCOPES = {"admin": "administrators", "user": "standard users", "disabled": "disabled accounts"}


def store_avatar(source):
    """Copy a chosen photo into the app's avatar folder as a square PNG.

    The account then keeps its picture even if the original file is moved.
    """
    reader = QImageReader(str(source))
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or "Unsupported image")
    side = min(image.width(), image.height())
    image = image.copy((image.width() - side) // 2, (image.height() - side) // 2, side, side)
    image = image.scaled(AVATAR_SIZE, AVATAR_SIZE, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    target = AVATAR_DIR / f"{secrets.token_hex(8)}.png"
    if not image.save(str(target), "PNG"):
        raise OSError(f"Could not write {target}")
    return str(target)


def discard_avatar(path):
    """Delete an avatar copy made by ``store_avatar``; other files are never touched."""
    if not path:
        return
    try:
        resolved = Path(path).resolve()
        if resolved.parent == AVATAR_DIR.resolve() and resolved.is_file():
            resolved.unlink()
    except OSError:
        pass


def format_date(value):
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value))
        if moment.tzinfo is not None:
            moment = moment.astimezone()
        return moment.strftime("%d %b %Y")
    except ValueError:
        return str(value)[:10]


@contextmanager
def _busy_cursor():
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


# ═════════════════════════════════════════════════════════════════════════════
#  Small shared pieces
# ═════════════════════════════════════════════════════════════════════════════

class _AccessMeter(QWidget):
    """Rounded bar showing granted permissions out of the total."""

    def __init__(self, theme="light", show_text=True, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._theme = theme
        self._show_text = show_text
        self._value = 0
        self._total = len(ALL_PERMISSIONS)
        self.setFixedHeight(20 if show_text else 8)
        self.setMinimumWidth(110 if show_text else 60)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, value, total=None):
        self._value = int(value)
        self._total = int(total or self._total)
        self.update()

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def paintEvent(self, event):
        c = palette(self._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        text_width = 44 if self._show_text else 0
        bar_width = max(20.0, min(96.0, self.width() - text_width)) if self._show_text else float(self.width())
        top = (self.height() - 6) / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c["border"]))
        painter.drawRoundedRect(QRectF(0, top, bar_width, 6), 3, 3)
        ratio = self._value / self._total if self._total else 0.0
        if ratio > 0:
            full = self._value >= self._total
            painter.setBrush(QColor(c["success" if full else "primary"]))
            painter.drawRoundedRect(QRectF(0, top, max(6.0, bar_width * ratio), 6), 3, 3)
        if self._show_text:
            painter.setPen(QColor(c["text_secondary"]))
            font = painter.font()
            font.setPixelSize(12)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(bar_width + 10, 0, text_width, self.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             f"{self._value}/{self._total}")
        painter.end()


def _role_pill(role):
    pill = QLabel(str(role or "user").upper())
    pill.setObjectName("rolePill")
    pill.setFixedHeight(20)
    pill.setProperty("role", "admin" if role == "admin" else "user")
    return pill


def _plain_widget():
    widget = QWidget()
    widget.setObjectName("plain")
    return widget


# ═════════════════════════════════════════════════════════════════════════════
#  Screen
# ═════════════════════════════════════════════════════════════════════════════

class UsersScreen(QWidget):
    usersChanged = pyqtSignal(list)

    COLUMNS = ("USER", "CONTACT", "ROLE", "STATUS", "ACCESS", "")

    def __init__(self, engine, settings, parent=None, current_user=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._theme = getattr(settings, "theme", "light")
        self._current_uuid = getattr(current_user, "uuid", None)
        self._cards = []
        self._stats = []
        self._users = []
        self._filter = "all"
        self._worker = None
        self._reload_pending = False
        self._build()
        self._refresh()

    # ── construction ─────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(16)

        self.header = PageHeader("", "Manage user accounts, roles and permissions.")
        self.add_button = QPushButton("  Add User")
        self.add_button.setObjectName("primary")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(self._add_user)
        self.header.add_action(self.add_button)
        outer.addWidget(self.header)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(12)
        self._st_total = StatCard("Total Accounts", "0", "", icon="users", tone="blue", theme=self._theme)
        self._st_admins = StatCard("Administrators", "0", "", icon="shield", tone="orange", theme=self._theme)
        self._st_users = StatCard("Standard Users", "0", "", icon="user", tone="green", theme=self._theme)
        self._st_disabled = StatCard("Disabled", "0", "", icon="user-x", tone="red", theme=self._theme)
        self._stats = [self._st_total, self._st_admins, self._st_users, self._st_disabled]
        for card in self._stats:
            stats_row.addWidget(card, 1)
        outer.addLayout(stats_row)

        table_card = Card("", theme=self._theme, divider=False)
        table_card.layout().setContentsMargins(1, 0, 1, 10)
        table_card.layout().setSpacing(0)
        table_card.body.setSpacing(0)
        table_card.header.hide()
        self._cards.append(table_card)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(16, 14, 16, 12)
        toolbar.setSpacing(10)
        self._search = TextInput("Search by name, username, email or phone…", icon="search",
                                 theme=self._theme)
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumHeight(38)
        self._search.textChanged.connect(self._apply_filter)
        toolbar.addWidget(self._search, 1)
        segments = QFrame()
        segments.setObjectName("segmentBar")
        segment_row = QHBoxLayout(segments)
        segment_row.setContentsMargins(3, 3, 3, 3)
        segment_row.setSpacing(2)
        self._segment_group = QButtonGroup(self)
        self._segments = {}
        for index, (key, label) in enumerate(FILTERS):
            button = QPushButton(label)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setChecked(key == self._filter)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._segment_group.addButton(button, index)
            self._segments[key] = button
            segment_row.addWidget(button)
        self._segment_group.idClicked.connect(self._on_segment)
        toolbar.addWidget(segments)
        self._refresh_btn = QPushButton("  Refresh")
        self._refresh_btn.setObjectName("controlButton")
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setMinimumHeight(38)
        self._refresh_btn.clicked.connect(self._refresh)
        toolbar.addWidget(self._refresh_btn)
        table_card.add_layout(toolbar)
        self._loading = ProgressLine(self._theme, height=2)
        table_card.add(self._loading)

        self._stack = QStackedWidget()
        self._stack.setObjectName("plain")
        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)
        self._table.setWordWrap(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(68)
        self._table.cellDoubleClicked.connect(self._on_double_click)
        header = self._table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in ((2, 104), (3, 112), (4, 140), (5, 142)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self._table.setColumnWidth(column, width)
        self._stack.addWidget(self._table)

        self._empty = EmptyState("No user accounts yet",
                                 "Create the first administrator. Once an account exists, "
                                 "the dashboard asks everyone to sign in.",
                                 icon="shield", theme=self._theme)
        self._empty.setObjectName("plain")
        first = self._empty.add_action("  Add first user", self._add_user)
        apply_button_icon(first, "user-plus", "#FFFFFF")
        self._stack.addWidget(self._empty)

        self._no_results = EmptyState("No matching users", "", icon="search", theme=self._theme)
        self._no_results.setObjectName("plain")
        self._no_results.add_action("Clear filters", self._clear_filters, object_name="controlButton")
        self._stack.addWidget(self._no_results)

        self._load_error = EmptyState("Couldn't load user accounts", "", icon="alert", theme=self._theme)
        self._load_error.setObjectName("plain")
        retry = self._load_error.add_action("  Try again", self._refresh, object_name="controlButton")
        apply_button_icon(retry, "refresh", palette(self._theme)["muted"], 14)
        self._stack.addWidget(self._load_error)
        table_card.add(self._stack, 1)
        outer.addWidget(table_card, 1)

        self.toast = ToastBar(theme=self._theme)
        outer.addWidget(self.toast)
        self._apply_icons()

    # ── data loading ─────────────────────────────────────────────────────

    def _refresh(self):
        if self._worker is not None and self._worker.isRunning():
            self._reload_pending = True
            return
        self._loading.start(0.4, 400)
        worker = _LoadWorker(self.settings)
        worker.loaded.connect(self._on_loaded)
        worker.failed.connect(self._on_load_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def _on_worker_finished(self):
        # ``finished`` arrives just before the thread exits; let it, so a pending
        # reload is not mistaken for "still running" and dropped.
        self._worker.wait()
        if self._reload_pending:
            self._reload_pending = False
            self._refresh()

    def _on_load_failed(self, message):
        self._loading.fail()
        self.toast.show_message(f"Couldn't load user accounts: {message}", "bad")
        if not self._users:
            # Never claim "no accounts yet" when the database simply did not answer.
            self._load_error.set_message("Couldn't load user accounts",
                                         "Check the database connection in Settings, then try again.")
            self._stack.setCurrentWidget(self._load_error)

    def _on_loaded(self, users):
        self._loading.finish("ok")
        self._users = list(users)
        admins = sum(1 for user in users if user.role == "admin")
        standard = len(users) - admins
        disabled = sum(1 for user in users if user.disabled)
        self._st_total.set_value(str(len(users)), "ok" if users else "idle")
        self._st_total.set_hint("registered accounts")
        self._st_admins.set_value(str(admins), "ok" if admins else "idle")
        self._st_admins.set_hint("full access")
        self._st_users.set_value(str(standard), "ok" if standard else "idle")
        self._st_users.set_hint("standard access")
        self._st_disabled.set_value(str(disabled), "bad" if disabled else "idle")
        self._st_disabled.set_hint("can't sign in" if disabled else "all accounts active")
        self._populate_table(self._users)
        self.usersChanged.emit(self._users)

    def _visible_users(self, users):
        query = self._search.text().strip().lower()
        if self._filter == "disabled":
            users = [user for user in users if user.disabled]
        elif self._filter != "all":
            users = [user for user in users if user.role == self._filter]
        if query:
            users = [user for user in users
                     if query in user.username.lower() or query in user.full_name.lower()
                     or query in user.email.lower() or query in user.phone.lower()]
        return users

    def _populate_table(self, users):
        visible = self._visible_users(users)
        self._table.setRowCount(0)
        if not users:
            self._stack.setCurrentWidget(self._empty)
            return
        if not visible:
            query = self._search.text().strip()
            scope = FILTER_SCOPES.get(self._filter, "accounts")
            self._no_results.set_message(
                "No matching users",
                f"No {scope} match “{query}”." if query else f"There are no {scope} yet.")
            self._stack.setCurrentWidget(self._no_results)
            return
        self._stack.setCurrentWidget(self._table)
        self._table.setRowCount(len(visible))
        for row, user in enumerate(visible):
            key = QTableWidgetItem()
            key.setData(Qt.ItemDataRole.UserRole, user.uuid)
            self._table.setItem(row, 0, key)
            self._table.setCellWidget(row, 0, self._user_cell(user))
            self._table.setCellWidget(row, 1, self._contact_cell(user))
            self._table.setCellWidget(row, 2, self._role_cell(user))
            self._table.setCellWidget(row, 3, self._status_cell(user))
            self._table.setCellWidget(row, 4, self._access_cell(user))
            self._table.setCellWidget(row, 5, self._action_cell(user))

    @staticmethod
    def _passthrough(widget):
        widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        return widget

    def _user_cell(self, user):
        cell = _plain_widget()
        row = QHBoxLayout(cell)
        row.setContentsMargins(14, 0, 8, 0)
        row.setSpacing(12)
        display = user.full_name or user.username
        avatar = Avatar(display, size=38, theme=self._theme, image=user.avatar_path)
        avatar.set_dimmed(user.disabled)
        row.addWidget(avatar)
        text = QVBoxLayout()
        text.setSpacing(1)
        text.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        name = QLabel(display)
        name.setObjectName("cellTitle")
        name.setProperty("dim", user.disabled)
        title_row.addWidget(name)
        if user.uuid == self._current_uuid:
            you = _role_pill("you")
            you.setText("YOU")
            you.setProperty("role", "you")
            title_row.addWidget(you)
        title_row.addStretch(1)
        text.addLayout(title_row)
        handle = QLabel(f"@{user.username}")
        handle.setObjectName("cellMeta")
        text.addWidget(handle)
        row.addLayout(text, 1)
        return self._passthrough(cell)

    def _contact_cell(self, user):
        cell = _plain_widget()
        column = QVBoxLayout(cell)
        column.setContentsMargins(8, 0, 8, 0)
        column.setSpacing(3)
        column.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        muted = palette(self._theme)["muted"]
        for glyph, value, blank in (("mail", user.email, "No email"), ("phone", user.phone, "No phone")):
            line = QHBoxLayout()
            line.setSpacing(7)
            line.addWidget(IconLabel(glyph, 13, muted))
            label = QLabel(value or blank)
            label.setObjectName("cellMeta")
            label.setProperty("empty", not value)
            line.addWidget(label, 1)
            column.addLayout(line)
        return self._passthrough(cell)

    def _role_cell(self, user):
        cell = _plain_widget()
        row = QHBoxLayout(cell)
        row.setContentsMargins(8, 0, 8, 0)
        pill = _role_pill(user.role)
        pill.setToolTip(role_title(user.role))
        row.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        return self._passthrough(cell)

    def _status_cell(self, user):
        cell = _plain_widget()
        row = QHBoxLayout(cell)
        row.setContentsMargins(8, 0, 8, 0)
        pill = QLabel("\u25CF  Disabled" if user.disabled else "\u25CF  Active")
        pill.setObjectName("statusPill")
        pill.setFixedHeight(22)
        pill.setProperty("state", "disabled" if user.disabled else "active")
        pill.setToolTip("Can't sign in" if user.disabled else "Can sign in")
        row.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)
        return self._passthrough(cell)

    def _access_cell(self, user):
        cell = _plain_widget()
        row = QHBoxLayout(cell)
        row.setContentsMargins(8, 0, 12, 0)
        meter = _AccessMeter(self._theme)
        meter.set_value(len(user.permissions), len(ALL_PERMISSIONS))
        row.addWidget(meter, 1, Qt.AlignmentFlag.AlignVCenter)
        names = [PERMISSIONS.get(perm, ("", perm))[1] for perm in user.permissions]
        cell.setToolTip("\n".join(names) if names else "No permissions")
        return self._passthrough(cell)

    def _action_cell(self, user):
        cell = _plain_widget()
        row = QHBoxLayout(cell)
        row.setContentsMargins(4, 0, 14, 0)
        row.setSpacing(4)
        row.addStretch(1)
        colors = palette(self._theme)
        edit = QPushButton()
        edit.setObjectName("iconButtonFlat")
        edit.setFixedSize(34, 34)
        edit.setIcon(make_icon("edit", 16, colors["primary"], ratio=2.0))
        edit.setToolTip("Edit user")
        edit.setAccessibleName(f"Edit {user.username}")
        edit.setCursor(Qt.CursorShape.PointingHandCursor)
        edit.clicked.connect(lambda _checked, uid=user.uuid: self._edit_user(uid))
        row.addWidget(edit)
        is_self = user.uuid == self._current_uuid
        toggle = QPushButton()
        toggle.setObjectName("iconButtonFlat")
        toggle.setFixedSize(34, 34)
        if user.disabled:
            toggle.setIcon(make_icon("user-check", 16, colors["success"], ratio=2.0))
            toggle.setToolTip("Enable user")
            toggle.setAccessibleName(f"Enable {user.username}")
        else:
            toggle.setIcon(make_icon("user-x", 16, colors["muted" if is_self else "warn"], ratio=2.0))
            toggle.setToolTip("You can't disable the account you're signed in with"
                              if is_self else "Disable user")
            toggle.setAccessibleName(f"Disable {user.username}")
        toggle.setEnabled(not is_self)
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.clicked.connect(lambda _checked, u=user: self._toggle_disabled(u))
        row.addWidget(toggle)
        delete = QPushButton()
        delete.setObjectName("iconButtonFlat")
        delete.setFixedSize(34, 34)
        delete.setIcon(make_icon("trash", 16, colors["muted" if is_self else "danger"], ratio=2.0))
        delete.setToolTip("You can't delete the account you're signed in with"
                          if is_self else "Delete user")
        delete.setAccessibleName(f"Delete {user.username}")
        delete.setEnabled(not is_self)
        delete.setCursor(Qt.CursorShape.PointingHandCursor)
        delete.clicked.connect(lambda _checked, u=user: self._delete_user(u))
        row.addWidget(delete)
        return cell

    def _apply_filter(self):
        self._populate_table(self._users)

    def _on_segment(self, index):
        self._set_filter(FILTERS[index][0])

    def _set_filter(self, key):
        self._filter = key
        self._segments[key].setChecked(True)
        self._populate_table(self._users)

    def _clear_filters(self):
        self._search.clear()
        self._set_filter("all")

    def _on_double_click(self, row, _column):
        item = self._table.item(row, 0)
        if item is not None:
            self._edit_user(item.data(Qt.ItemDataRole.UserRole))

    # ── CRUD dialogs ─────────────────────────────────────────────────────

    def _add_user(self):
        dialog = UserDialog(theme=self._theme, first_user=not self._users, parent=self,
                            existing_usernames=[user.username for user in self._users])
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.result_data()
        try:
            with _busy_cursor():
                UserRepository(self.settings).create_user(
                    username=data["username"], full_name=data["full_name"],
                    phone=data["phone"], email=data["email"],
                    password=data["password"], role=data["role"],
                    permissions=data["permissions"], avatar_path=data["avatar_path"])
        except Exception as exc:
            discard_avatar(dialog.new_avatar_copy())
            self.toast.show_message(f"Failed to create user: {exc}", "bad")
            return
        self.toast.show_message(
            f"{data['full_name'] or data['username']} was added as {role_title(data['role']).lower()}.", "ok")
        self._refresh()

    def _edit_user(self, uid):
        user = next((candidate for candidate in self._users if candidate.uuid == uid), None)
        if user is None:
            return
        dialog = UserDialog(user=user, theme=self._theme, parent=self,
                            existing_usernames=[other.username for other in self._users],
                            is_self=uid == self._current_uuid)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.result_data()
        try:
            with _busy_cursor():
                UserRepository(self.settings).update_user(
                    uid, username=data["username"], full_name=data["full_name"],
                    phone=data["phone"], email=data["email"],
                    role=data["role"], permissions=data["permissions"],
                    avatar_path=data["avatar_path"], disabled=data["disabled"],
                    password=data["password"] if data["password"] else None)
        except Exception as exc:
            discard_avatar(dialog.new_avatar_copy())
            self.toast.show_message(f"Failed to update user: {exc}", "bad")
            return
        if user.avatar_path and user.avatar_path != data["avatar_path"]:
            discard_avatar(user.avatar_path)
        name = data["full_name"] or data["username"]
        if data["disabled"] and not user.disabled:
            self.toast.show_message(f"Saved changes. {name} can no longer sign in.", "warn")
        elif user.disabled and not data["disabled"]:
            self.toast.show_message(f"Saved changes. {name} can sign in again.", "ok")
        else:
            self.toast.show_message(f"Saved changes to {name}.", "ok")
        self._refresh()

    def _toggle_disabled(self, user):
        if user.uuid == self._current_uuid:
            return
        name = user.full_name or user.username
        disable = not user.disabled
        if disable:
            box = QMessageBox(QMessageBox.Icon.Warning, "Disable user",
                              f"Disable {name} (@{user.username})?", parent=self)
            box.setInformativeText(
                "They won't be able to sign in, and a session that is already open ends "
                "within a minute. Their details and attendance history are kept, and you "
                "can enable the account again at any time.")
            confirm = box.addButton("Disable user", QMessageBox.ButtonRole.DestructiveRole)
            cancel = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(cancel)
            box.exec()
            if box.clickedButton() is not confirm:
                return
        try:
            with _busy_cursor():
                UserRepository(self.settings).set_disabled(user.uuid, disable)
        except Exception as exc:
            verb = "disable" if disable else "enable"
            self.toast.show_message(f"Failed to {verb} {name}: {exc}", "bad")
            return
        if disable:
            self.toast.show_message(f"{name} was disabled and can no longer sign in.", "warn")
        else:
            self.toast.show_message(f"{name} was enabled and can sign in again.", "ok")
        self._refresh()

    def _delete_user(self, user):
        name = user.full_name or user.username
        box = QMessageBox(QMessageBox.Icon.Warning, "Delete user",
                          f"Delete {name} (@{user.username})?", parent=self)
        box.setInformativeText("They will no longer be able to sign in. This can't be undone.")
        delete = box.addButton("Delete user", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton() is not delete:
            return
        try:
            with _busy_cursor():
                UserRepository(self.settings).delete_user(user.uuid)
        except Exception as exc:
            self.toast.show_message(f"Failed to delete user: {exc}", "bad")
            return
        discard_avatar(user.avatar_path)
        self.toast.show_message(f"{name} was deleted.", "ok")
        self._refresh()

    # ── theme / settings ─────────────────────────────────────────────────

    def set_current_user(self, user):
        self._current_uuid = getattr(user, "uuid", None)
        self._populate_table(self._users)

    def on_settings_changed(self, settings):
        self.settings = settings

    def set_theme(self, theme):
        self._theme = theme
        self.toast.set_theme(theme)
        self._loading.set_theme(theme)
        self._search.set_theme(theme)
        for state in (self._empty, self._no_results, self._load_error):
            state.set_theme(theme)
        for card in self._cards:
            card.set_theme(theme)
        for card in self._stats:
            card.set_theme(theme)
        self._apply_icons()
        self._populate_table(self._users)

    def _apply_icons(self):
        colors = palette(self._theme)
        apply_button_icon(self.add_button, "user-plus", "#FFFFFF")
        apply_button_icon(self._refresh_btn, "refresh", colors["muted"], 14)

    def closeEvent(self, event):
        settle(self._worker)
        super().closeEvent(event)


# ═════════════════════════════════════════════════════════════════════════════
#  Add / Edit dialog
# ═════════════════════════════════════════════════════════════════════════════

class _PhotoAvatar(Avatar):
    clicked = pyqtSignal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Change photo")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class UserDialog(QDialog):
    """Create or edit an account: live preview on the left, sectioned form on the right."""

    def __init__(self, user=None, theme="light", first_user=False, parent=None,
                 existing_usernames=(), is_self=False):
        super().__init__(parent)
        self._user = user
        self._editing = user is not None
        # Editing your own account: nothing here may lock you out of it.
        self._is_self = bool(is_self and user is not None)
        self._theme = theme
        self._first_user = first_user and not self._editing
        self._original_avatar = (user.avatar_path if user else "") or ""
        self._avatar_source = self._original_avatar
        self._stored_avatar = None
        own = user.username.lower() if user else None
        self._taken = {name.lower() for name in existing_usernames if name and name.lower() != own}
        self._role = user.role if user else ("admin" if self._first_user else "user")
        self.setWindowTitle("Edit User" if self._editing else "Add New User")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setMinimumSize(900, 640)
        self.resize(940, 700)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_identity())
        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        main.addWidget(self._build_header())
        main.addWidget(self._build_form(), 1)
        main.addWidget(self._build_footer())
        root.addLayout(main, 1)

        if user:
            self.full_name.setText(user.full_name)
            self.username.setText(user.username)
            self.email.setText(user.email)
            self.phone.setText(user.phone)
        self._select_role(self._role, apply_defaults=False)
        self._set_permissions(user.permissions if user else ROLE_DEFAULTS.get(self._role, []))
        for editor in (self.full_name, self.username):
            editor.textChanged.connect(self._refresh_preview)
        self.password.textChanged.connect(self._on_password_changed)
        self.confirm.textChanged.connect(self._check_match)
        self._refresh_preview()
        if not self._editing:
            # Editing opens without a focused field so nothing starts selected.
            self.full_name.setFocus()

    # --- layout ------------------------------------------------------------------
    def _build_identity(self):
        c = palette(self._theme)
        panel = QFrame()
        panel.setObjectName("identityPanel")
        panel.setFixedWidth(284)
        column = QVBoxLayout(panel)
        column.setContentsMargins(26, 28, 26, 24)
        column.setSpacing(0)
        eyebrow = QLabel("PROFILE PREVIEW")
        eyebrow.setObjectName("eyebrow")
        column.addWidget(eyebrow, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(20)
        self.preview_avatar = _PhotoAvatar("?", size=112, theme=self._theme,
                                           image=self._avatar_source, ring=True)
        self.preview_avatar.clicked.connect(self._pick_avatar)
        column.addWidget(self.preview_avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(14)
        photo_row = QHBoxLayout()
        photo_row.setSpacing(6)
        photo_row.addStretch(1)
        self.upload_button = QPushButton("  Upload photo")
        self.upload_button.setObjectName("controlButton")
        self.upload_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upload_button.setAutoDefault(False)
        apply_button_icon(self.upload_button, "upload", c["primary"], 14)
        self.upload_button.clicked.connect(self._pick_avatar)
        photo_row.addWidget(self.upload_button)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("ghostButton")
        self.remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(self._remove_avatar)
        photo_row.addWidget(self.remove_button)
        photo_row.addStretch(1)
        column.addLayout(photo_row)
        column.addSpacing(6)
        self.photo_hint = QLabel("PNG or JPG • cropped to a square")
        self.photo_hint.setObjectName("fieldHelp")
        self.photo_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.photo_hint)
        column.addSpacing(22)

        self.preview_name = QLabel("")
        self.preview_name.setObjectName("identityName")
        self.preview_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.preview_name)
        column.addSpacing(2)
        self.preview_handle = QLabel("")
        self.preview_handle.setObjectName("identityHandle")
        self.preview_handle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.preview_handle)
        column.addSpacing(10)
        self.preview_role = _role_pill(self._role)
        column.addWidget(self.preview_role, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(24)

        access_head = QHBoxLayout()
        access_label = QLabel("Access")
        access_label.setObjectName("fieldLabel")
        access_head.addWidget(access_label)
        access_head.addStretch(1)
        self.preview_count = QLabel("")
        self.preview_count.setObjectName("fieldHelp")
        access_head.addWidget(self.preview_count)
        column.addLayout(access_head)
        column.addSpacing(8)
        self.preview_meter = _AccessMeter(self._theme, show_text=False)
        column.addWidget(self.preview_meter)
        column.addStretch(1)

        tip = QFrame()
        tip.setObjectName("tipBox")
        tip_row = QHBoxLayout(tip)
        tip_row.setContentsMargins(12, 12, 12, 12)
        tip_row.setSpacing(10)
        tip_row.addWidget(IconLabel("info", 16, c["primary"]), 0, Qt.AlignmentFlag.AlignTop)
        self.tip_label = QLabel("")
        self.tip_label.setObjectName("tipText")
        self.tip_label.setWordWrap(True)
        tip_row.addWidget(self.tip_label, 1)
        column.addWidget(tip)
        return panel

    def _build_header(self):
        header = QFrame()
        header.setObjectName("dialogHeader")
        column = QVBoxLayout(header)
        column.setContentsMargins(28, 22, 28, 18)
        column.setSpacing(3)
        if self._editing:
            title, subtitle = "Edit user", "Update account details, credentials and access."
        elif self._first_user:
            title, subtitle = ("Create the first administrator",
                               "This account will be required to sign in to the dashboard.")
        else:
            title, subtitle = "Create a new user", "Add a team member and choose what they can access."
        title_label = QLabel(title)
        title_label.setObjectName("dialogTitle")
        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("dialogSubtitle")
        column.addWidget(title_label)
        column.addWidget(subtitle_label)
        return header

    def _build_form(self):
        theme = self._theme
        self.scroll = QScrollArea()
        self.scroll.setObjectName("dialogBody")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("dialogBody")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 22, 28, 26)
        layout.setSpacing(14)

        layout.addWidget(SectionHeading("Personal details"))
        self.full_name = TextInput("e.g. Sokha Chan", icon="user", theme=theme)
        self.email = TextInput("name@company.com", icon="mail", theme=theme)
        self.phone = TextInput("+855 12 345 678", icon="phone", theme=theme)
        self.full_name_field = FormField("Full name", self.full_name, optional=True, theme=theme)
        self.email_field = FormField("Email", self.email, optional=True, theme=theme)
        self.phone_field = FormField("Phone", self.phone, optional=True, theme=theme)
        personal = QGridLayout()
        personal.setHorizontalSpacing(14)
        personal.setVerticalSpacing(12)
        top = Qt.AlignmentFlag.AlignTop
        personal.addWidget(self.full_name_field, 0, 0, 1, 2)
        personal.addWidget(self.email_field, 1, 0, top)
        personal.addWidget(self.phone_field, 1, 1, top)
        layout.addLayout(personal)
        layout.addSpacing(10)

        layout.addWidget(SectionHeading(
            "Sign-in credentials",
            "Leave the password blank to keep the current one." if self._editing
            else "Share these with the person securely; they can change the password later."))
        self.username = TextInput("e.g. sokha", icon="at", theme=theme)
        self.username_field = FormField("Username", self.username,
                                        help_text="At least 3 characters, no spaces.", theme=theme)
        layout.addWidget(self.username_field)
        self.password = TextInput("Leave blank to keep current" if self._editing
                                  else "At least 6 characters", icon="lock", password=True, theme=theme)
        self.confirm = TextInput("Re-enter the password", icon="lock", password=True, theme=theme)
        self.password_field = FormField("New password" if self._editing else "Password",
                                        self.password, theme=theme)
        self.confirm_field = FormField("Confirm password", self.confirm, theme=theme)
        self.strength = StrengthMeter(theme)
        secrets_grid = QGridLayout()
        secrets_grid.setHorizontalSpacing(14)
        secrets_grid.setVerticalSpacing(8)
        secrets_grid.addWidget(self.password_field, 0, 0, top)
        secrets_grid.addWidget(self.confirm_field, 0, 1, top)
        secrets_grid.addWidget(self.strength, 1, 0)
        layout.addLayout(secrets_grid)
        layout.addSpacing(10)

        roles_heading = SectionHeading(
            "Role & permissions",
            "Choosing a role applies its default permissions; fine-tune them below.")
        self.reset_button = QPushButton("Reset to role defaults")
        self.reset_button.setObjectName("linkButton")
        self.reset_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset_button.setAutoDefault(False)
        self.reset_button.clicked.connect(self._reset_permissions)
        roles_heading.trailing.addWidget(self.reset_button)
        layout.addWidget(roles_heading)
        role_row = QHBoxLayout()
        role_row.setSpacing(12)
        self.role_cards = {}
        for key, (glyph, title, body, tone) in ROLE_CHOICES.items():
            card = ChoiceCard(glyph, title, body, tone=tone, theme=theme)
            card.setProperty("role", key)
            card.clicked.connect(self._on_role_card)
            role_row.addWidget(card, 1)
            self.role_cards[key] = card
        if self._first_user:
            self.role_cards["user"].setEnabled(False)
            self.role_cards["user"].setToolTip("The first account must be an administrator.")
        elif self._is_self:
            for key, card in self.role_cards.items():
                if key != self._role:
                    card.setEnabled(False)
                    card.setToolTip("You can't change the role of the account you're signed in with.")
        layout.addLayout(role_row)

        permissions = QGridLayout()
        permissions.setHorizontalSpacing(12)
        permissions.setVerticalSpacing(10)
        self.perm_tiles = {}
        for index, perm in enumerate(ALL_PERMISSIONS):
            glyph, title, body, tone = PERMISSIONS.get(perm, ("dot", perm, "", "blue"))
            tile = PermissionTile(glyph, title, body, tone=tone, theme=theme)
            tile.toggled.connect(self._refresh_preview)
            row, column = divmod(index, 2)
            permissions.addWidget(tile, row, column)
            self.perm_tiles[perm] = tile
        if self._is_self:
            keep = self.perm_tiles["manage_users"]
            keep.setEnabled(False)
            keep.setToolTip("You can't remove your own access to user management.")
        layout.addLayout(permissions)

        self.status_tile = None
        if self._editing and not self._is_self:
            layout.addSpacing(10)
            layout.addWidget(SectionHeading(
                "Account status",
                "Disabled accounts can't sign in. Their details and attendance history are kept."))
            self.status_tile = PermissionTile("user-check", "", "", tone="green",
                                              checked=not self._user.disabled, theme=theme)
            self.status_tile.toggled.connect(self._on_status_toggled)
            self._on_status_toggled(self.status_tile.isChecked())
            layout.addWidget(self.status_tile)
        layout.addStretch(1)
        self.scroll.setWidget(content)
        return self.scroll

    def _on_status_toggled(self, enabled):
        tile = self.status_tile
        tile.tile.set_icon("user-check" if enabled else "user-x")
        tile.title_label.setText("Can sign in" if enabled else "Sign-in disabled")
        tile.body_label.setText("Turn off to disable this account"
                                if enabled else "Turn on to let this person sign in again")
        self.preview_avatar.set_dimmed(not enabled)

    def _build_footer(self):
        c = palette(self._theme)
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        row = QHBoxLayout(footer)
        row.setContentsMargins(28, 14, 28, 14)
        row.setSpacing(10)
        self.error_icon = IconLabel("alert", 16, c["danger"])
        self.error_icon.hide()
        row.addWidget(self.error_icon)
        self.error_label = QLabel("")
        self.error_label.setObjectName("fieldHelp")
        self.error_label.setProperty("tone", "bad")
        self.error_label.hide()
        row.addWidget(self.error_label)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setMinimumHeight(40)
        cancel.setAutoDefault(False)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.save_button = QPushButton("  Save changes" if self._editing else "  Create user")
        self.save_button.setObjectName("primary")
        self.save_button.setMinimumHeight(40)
        self.save_button.setAutoDefault(False)
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_button_icon(self.save_button, "check" if self._editing else "user-plus", "#FFFFFF")
        self.save_button.clicked.connect(self._validate_and_accept)
        row.addWidget(self.save_button)
        return footer

    # --- behaviour -----------------------------------------------------------------
    def _on_role_card(self):
        self._select_role(self.sender().property("role"))

    def _reset_permissions(self):
        self._set_permissions(ROLE_DEFAULTS.get(self._role, []))

    def _select_role(self, role, apply_defaults=True):
        self._role = role
        for key, card in self.role_cards.items():
            card.setChecked(key == role)
        if apply_defaults:
            self._set_permissions(ROLE_DEFAULTS.get(role, []))
        self.tip_label.setText(ROLE_TIPS.get(role, ""))
        self._refresh_preview()

    def _set_permissions(self, granted):
        granted = set(granted)
        if self._is_self:
            granted.add("manage_users")
        for perm, tile in self.perm_tiles.items():
            tile.setChecked(perm in granted)
        self._refresh_preview()

    def _granted(self):
        return [perm for perm, tile in self.perm_tiles.items() if tile.isChecked()]

    def _refresh_preview(self, *_):
        full_name = self.full_name.text().strip()
        username = self.username.text().strip()
        display = full_name or username or ("New administrator" if self._first_user else "New user")
        self.preview_avatar.set_name(full_name or username or "?")
        self.preview_name.setText(self.preview_name.fontMetrics().elidedText(
            display, Qt.TextElideMode.ElideRight, 228))
        self.preview_handle.setText(f"@{username}" if username else "@username")
        self.preview_role.setText(role_title(self._role).upper())
        repolish(self.preview_role, "role", "admin" if self._role == "admin" else "user")
        granted = len(self._granted())
        total = len(ALL_PERMISSIONS)
        self.preview_meter.set_value(granted, total)
        self.preview_count.setText(f"{granted} of {total} permissions")
        self.remove_button.setVisible(bool(self._avatar_source))

    def _on_password_changed(self, text):
        self.strength.set_password(text)
        self._check_match()

    def _check_match(self, *_):
        confirm = self.confirm.text()
        if not confirm:
            self.confirm_field.set_hint("")
        elif confirm == self.password.text():
            self.confirm_field.set_hint("Passwords match", "ok")
        else:
            self.confirm_field.set_hint("Passwords don't match yet", "warn")

    def _pick_avatar(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a profile photo", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not path:
            return
        if load_avatar(path).isNull():
            self._show_footer_error("That file couldn't be opened as an image.")
            return
        self._avatar_source = path
        self.preview_avatar.set_image(path)
        self.photo_hint.setText(Path(path).name)
        self._refresh_preview()

    def _remove_avatar(self):
        self._avatar_source = ""
        self.preview_avatar.set_image(None)
        self.photo_hint.setText("PNG or JPG • cropped to a square")
        self._refresh_preview()

    def _show_footer_error(self, message):
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))
        self.error_icon.setVisible(bool(message))

    def _validate_and_accept(self):
        invalid = []

        def reject(field, message):
            field.set_error(message)
            invalid.append(field)

        username = self.username.text().strip()
        original = self._user.username if self._user else None
        if not username:
            reject(self.username_field, "Username is required.")
        elif len(username) < 3:
            reject(self.username_field, "Use at least 3 characters.")
        elif username != original and any(char.isspace() for char in username):
            reject(self.username_field, "Usernames can't contain spaces.")
        elif username.lower() in self._taken:
            reject(self.username_field, "That username is already taken.")

        email = self.email.text().strip()
        original_email = self._user.email if self._user else ""
        if email and email != original_email and not EMAIL_PATTERN.match(email):
            reject(self.email_field, "Enter a valid email address.")

        password = self.password.text()
        if not self._editing and not password:
            reject(self.password_field, "Set a password for the new account.")
        elif password and len(password) < 6:
            reject(self.password_field, "Use at least 6 characters.")
        if password and password != self.confirm.text():
            reject(self.confirm_field, "Passwords don't match.")

        if invalid:
            count = len(invalid)
            self._show_footer_error(f"Please fix {count} highlighted field{'s' if count != 1 else ''}.")
            self.scroll.ensureWidgetVisible(invalid[0], 0, 60)
            invalid[0].editor.setFocus()
            return
        self._show_footer_error("")
        if self._avatar_source and self._avatar_source != self._original_avatar:
            try:
                self._stored_avatar = store_avatar(self._avatar_source)
            except (OSError, ValueError):
                self._stored_avatar = None
        self.accept()

    def new_avatar_copy(self):
        """The avatar file saved for this dialog, if any (to clean up on failure)."""
        return self._stored_avatar

    def result_data(self):
        if self._avatar_source == self._original_avatar:
            avatar = self._original_avatar
        elif not self._avatar_source:
            avatar = ""
        else:
            avatar = self._stored_avatar or self._avatar_source
        return {
            "full_name": self.full_name.text().strip(),
            "username": self.username.text().strip(),
            "email": self.email.text().strip(),
            "phone": self.phone.text().strip(),
            "role": self._role,
            "password": self.password.text(),
            "permissions": self._granted(),
            "avatar_path": avatar,
            "disabled": (not self.status_tile.isChecked() if self.status_tile is not None
                         else bool(self._user and self._user.disabled)),
        }


# ═════════════════════════════════════════════════════════════════════════════
#  Profile dialog — opened from the header account menu
# ═════════════════════════════════════════════════════════════════════════════

class _ProfileBanner(QWidget):
    """Gradient cover with the avatar straddling its lower edge."""

    COVER = 112

    def __init__(self, user, theme="light", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setFixedHeight(self.COVER + 58)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, self.COVER - 52, 0, 0)
        self.avatar = Avatar(user.full_name or user.username, size=104, theme=theme,
                             image=user.avatar_path, ring=True)
        layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

    def paintEvent(self, event):
        c = palette(self._theme)
        w, h = float(self.width()), float(self.COVER)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(c["card"]))
        cover = QLinearGradient(0, 0, w, h)
        cover.setColorAt(0.0, QColor("#2563EB"))
        cover.setColorAt(0.6, QColor("#1E3A8A"))
        cover.setColorAt(1.0, QColor("#312E81"))
        painter.fillRect(QRectF(0, 0, w, h), cover)
        glow = QRadialGradient(w * 0.85, 0, w * 0.55)
        glow.setColorAt(0.0, QColor(147, 197, 253, 110))
        glow.setColorAt(1.0, QColor(147, 197, 253, 0))
        painter.fillRect(QRectF(0, 0, w, h), glow)
        painter.setClipRect(QRectF(0, 0, w, h))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index, radius in enumerate((70, 110, 150)):
            ring = QColor(255, 255, 255, 30 - index * 8)
            painter.setPen(ring)
            painter.drawEllipse(QRectF(w - 60 - radius, -radius * 0.6, radius * 2, radius * 2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 16))
        for x in range(16, int(w * 0.45), 18):
            for y in range(14, int(h), 18):
                painter.drawEllipse(QRectF(x, y, 2, 2))
        painter.end()


class ProfileDialog(QDialog):
    def __init__(self, user, settings, theme="light", parent=None):
        super().__init__(parent)
        self._user = user
        self._settings = settings
        self._theme = theme
        self.setWindowTitle("My Profile")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setMinimumSize(560, 680)
        self.resize(580, 740)
        c = palette(theme)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setObjectName("dialogBody")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("dialogBody")
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self.banner = _ProfileBanner(user, theme)
        column.addWidget(self.banner)
        name = QLabel(user.full_name or user.username)
        name.setObjectName("identityName")
        name.setStyleSheet("font-size: 20px;")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addSpacing(8)
        column.addWidget(name)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        meta.addStretch(1)
        handle = QLabel(f"@{user.username}")
        handle.setObjectName("identityHandle")
        meta.addWidget(handle)
        pill = _role_pill(user.role)
        pill.setText(role_title(user.role).upper())
        meta.addWidget(pill)
        meta.addStretch(1)
        column.addSpacing(4)
        column.addLayout(meta)

        body = QVBoxLayout()
        body.setContentsMargins(28, 22, 28, 24)
        body.setSpacing(14)
        facts = QGridLayout()
        facts.setHorizontalSpacing(12)
        facts.setVerticalSpacing(12)
        for index, (glyph, tone, label, value) in enumerate((
                ("mail", "blue", "Email", user.email or "Not set"),
                ("phone", "green", "Phone", user.phone or "Not set"),
                ("calendar", "purple", "Member since", format_date(user.created_at)),
                ("clock", "orange", "Last updated", format_date(user.updated_at)))):
            facts.addWidget(self._info_tile(glyph, tone, label, value), *divmod(index, 2))
        body.addLayout(facts)
        body.addSpacing(6)

        granted = set(user.permissions)
        access = SectionHeading("Access")
        count = QLabel(f"{len(granted & set(ALL_PERMISSIONS))} of {len(ALL_PERMISSIONS)}")
        count.setObjectName("fieldHelp")
        access.trailing.addWidget(count)
        body.addWidget(access)
        chips = QGridLayout()
        chips.setHorizontalSpacing(10)
        chips.setVerticalSpacing(8)
        for index, perm in enumerate(ALL_PERMISSIONS):
            glyph, title, _body, _tone = PERMISSIONS.get(perm, ("dot", perm, "", "blue"))
            chips.addWidget(self._perm_chip(title, perm in granted), *divmod(index, 2))
        body.addLayout(chips)
        body.addSpacing(6)

        body.addWidget(SectionHeading("Security", "Use at least 6 characters. Longer is stronger."))
        self._pw_current = TextInput("Current password", icon="lock", password=True, theme=theme)
        self._pw_new = TextInput("New password", icon="key", password=True, theme=theme)
        self._pw_confirm = TextInput("Repeat new password", icon="key", password=True, theme=theme)
        self._current_field = FormField("Current password", self._pw_current, theme=theme)
        self._new_field = FormField("New password", self._pw_new, theme=theme)
        self._confirm_field = FormField("Confirm new password", self._pw_confirm, theme=theme)
        body.addWidget(self._current_field)
        passwords = QGridLayout()
        passwords.setHorizontalSpacing(12)
        passwords.setVerticalSpacing(8)
        passwords.addWidget(self._new_field, 0, 0, Qt.AlignmentFlag.AlignTop)
        passwords.addWidget(self._confirm_field, 0, 1, Qt.AlignmentFlag.AlignTop)
        self._strength = StrengthMeter(theme)
        passwords.addWidget(self._strength, 1, 0)
        body.addLayout(passwords)
        self._pw_new.textChanged.connect(self._strength.set_password)

        action = QHBoxLayout()
        action.setSpacing(10)
        self._pw_msg = QLabel("")
        self._pw_msg.setObjectName("fieldHelp")
        self._pw_msg.setWordWrap(True)
        self._pw_msg.setVisible(False)
        action.addWidget(self._pw_msg, 1)
        self._pw_button = QPushButton("  Update password")
        self._pw_button.setObjectName("softButton")
        self._pw_button.setMinimumHeight(38)
        self._pw_button.setAutoDefault(False)
        self._pw_button.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_button_icon(self._pw_button, "check", c["primary_soft_fg"], 14)
        self._pw_button.clicked.connect(self._change_password)
        action.addWidget(self._pw_button, 0, Qt.AlignmentFlag.AlignTop)
        body.addLayout(action)
        body.addStretch(1)
        column.addLayout(body, 1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        footer = QFrame()
        footer.setObjectName("dialogFooter")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(24, 12, 24, 12)
        footer_row.addStretch(1)
        close = QPushButton("Close")
        close.setMinimumHeight(38)
        close.setAutoDefault(False)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        footer_row.addWidget(close)
        outer.addWidget(footer)

    def _info_tile(self, glyph, tone, label, value):
        tile = QFrame()
        tile.setObjectName("infoTile")
        row = QHBoxLayout(tile)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(10)
        row.addWidget(IconTile(glyph, tone=tone, size=34, theme=self._theme))
        text = QVBoxLayout()
        text.setSpacing(1)
        caption = QLabel(label)
        caption.setObjectName("infoLabel")
        shown = QLabel(value)
        shown.setObjectName("infoValue")
        shown.setToolTip(value)
        shown.setMinimumWidth(0)
        shown.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        text.addWidget(caption)
        text.addWidget(shown)
        row.addLayout(text, 1)
        return tile

    def _perm_chip(self, title, granted):
        c = palette(self._theme)
        chip = QFrame()
        chip.setObjectName("permChip")
        chip.setProperty("granted", granted)
        row = QHBoxLayout(chip)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(8)
        row.addWidget(IconLabel("check-circle" if granted else "lock", 15,
                                c["success"] if granted else c["muted"]))
        text = QLabel(title)
        text.setObjectName("permChipText")
        row.addWidget(text, 1)
        return chip

    def _password_message(self, text, tone="bad"):
        self._pw_msg.setText(text)
        repolish(self._pw_msg, "tone", tone)
        self._pw_msg.setVisible(bool(text))

    def _change_password(self):
        for field in (self._current_field, self._new_field, self._confirm_field):
            field.clear_error()
        current = self._pw_current.text()
        new = self._pw_new.text()
        if not current:
            self._current_field.set_error("Enter your current password.")
            self._pw_current.setFocus()
            return
        if not verify_password(current, self._user.password_hash):
            self._current_field.set_error("Current password is incorrect.")
            self._pw_current.setFocus()
            return
        if len(new) < 6:
            self._new_field.set_error("Use at least 6 characters.")
            self._pw_new.setFocus()
            return
        if new != self._pw_confirm.text():
            self._confirm_field.set_error("Passwords don't match.")
            self._pw_confirm.setFocus()
            return
        try:
            with _busy_cursor():
                repository = UserRepository(self._settings)
                repository.update_user(self._user.uuid, password=new)
                self._user = repository.get_user(self._user.uuid) or self._user
        except Exception as exc:
            self._password_message(f"Couldn't update the password: {exc}")
            return
        self._password_message("Password updated. Use it the next time you sign in.", "ok")
        for editor in (self._pw_current, self._pw_new, self._pw_confirm):
            editor.clear()


# ═════════════════════════════════════════════════════════════════════════════
#  Background worker
# ═════════════════════════════════════════════════════════════════════════════

class _LoadWorker(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, settings):
        super().__init__()
        self._settings = settings

    def run(self):
        try:
            users = UserRepository(self._settings).list_users()
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)
            return
        self.loaded.emit(users)
