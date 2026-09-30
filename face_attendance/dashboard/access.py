"""Role-based access for the dashboard.

``AccessControl`` answers "may the signed-in account do X?".  Navigation
entries the account cannot open are hidden; any other route into them, and
guarded actions inside a screen, show an "Access denied" notice instead.
Without an account (no users yet, or PIN-only sign-in) everything is allowed,
exactly as before accounts existed.

Screens do not need a reference to the policy: ``guard(widget, ...)`` finds it
through the widget's window, so a screen built on its own (as in tests) simply
has full access.
"""
from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from ..users import UserRepository
from .threads import settle

# permission -> (glyph, title, description, tile tone)
PERMISSION_INFO = {
    "view_dashboard": ("monitor", "View dashboard", "Live monitor, usage and notifications", "blue"),
    "view_reports": ("chart", "View reports", "Browse and filter attendance", "green"),
    "manage_employees": ("users", "Manage employees", "Enroll people and delete records", "purple"),
    "manage_users": ("shield", "Manage users", "Accounts, roles and access", "orange"),
    "manage_settings": ("gear", "Manage settings", "Camera, recognition and alerts", "blue"),
    "export_data": ("download", "Export data", "Exports, snapshots and backups", "green"),
    "control_engine": ("bolt", "Control engine", "Start, pause and restart capture", "red"),
}

# Navigation title (``app.NAV``) -> permission needed to open that screen.
NAV_PERMISSIONS = {
    "Live Monitor": "view_dashboard",
    "Attendance Report": "view_reports",
    "Enrolled Employees": "manage_employees",
    "Live Usage": "view_dashboard",
    "Users & Access": "manage_users",
    "Settings": "manage_settings",
}

ROLE_TITLES = {"admin": "Administrator", "user": "Standard user"}


def permission_title(permission):
    info = PERMISSION_INFO.get(permission)
    return info[1] if info else str(permission).replace("_", " ").capitalize()


class AccessControl(QObject):
    """What the signed-in account may open or do; no account means full access."""

    changed = pyqtSignal()

    def __init__(self, user=None, theme="light", parent=None):
        super().__init__(parent)
        self._user = user
        self.theme = theme
        self._notice = None

    @property
    def user(self):
        return self._user

    def set_user(self, user):
        self._user = user
        self.changed.emit()

    def restricted(self):
        return self._user is not None

    def allows(self, permission):
        return self._user is None or permission in (self._user.permissions or [])

    def allows_nav(self, title):
        permission = NAV_PERMISSIONS.get(title)
        return permission is None or self.allows(permission)

    def account_label(self):
        if self._user is None:
            return ""
        name = self._user.full_name or self._user.username
        role = ROLE_TITLES.get(self._user.role, str(self._user.role).title())
        return f"Signed in as {name} • {role}"

    def deny(self, permission, action, widget=None):
        """Explain, over the window of *widget*, that *action* needs *permission*."""
        from .widgets.access_denied import AccessDeniedDialog
        if self._notice is not None and self._notice.isVisible():
            self._notice.raise_()
            return self._notice
        host = widget.window() if widget is not None else None
        notice = AccessDeniedDialog(action, permission_title(permission), self.account_label(),
                                    theme=self.theme, parent=host)
        notice.finished.connect(self._notice_closed)
        notice.finished.connect(notice.deleteLater)
        self._notice = notice
        notice.open()
        return notice

    def _notice_closed(self, _result):
        self._notice = None

    def notice(self):
        """The access-denied notice currently on screen, if any."""
        return self._notice if self._notice is not None and self._notice.isVisible() else None


def access_for(widget):
    """The ``AccessControl`` of the window hosting *widget* (dialogs defer to their parent)."""
    while widget is not None:
        window = widget.window()
        access = getattr(window, "access", None)
        if isinstance(access, AccessControl):
            return access
        widget = window.parentWidget()
    return None


def guard(widget, permission, action):
    """True when the signed-in account may *action*; otherwise show why not and return False."""
    access = access_for(widget)
    if access is None or access.allows(permission):
        return True
    access.deny(permission, action, widget)
    return False


class _AccountCheck(QThread):
    checked = pyqtSignal(object, bool)   # fresh user (None when removed), query succeeded

    def __init__(self, settings, uuid):
        super().__init__()
        self._settings = settings
        self._uuid = uuid

    def run(self):
        try:
            user = UserRepository(self._settings).get_user(self._uuid)
        except Exception:
            self.checked.emit(None, False)
            return
        self.checked.emit(user, True)


def _signature(user):
    if user is None:
        return None
    return (user.disabled, user.role, tuple(sorted(user.permissions or [])), user.username,
            user.full_name, user.email, user.avatar_path)


class AccountWatcher(QObject):
    """Re-reads the signed-in account on a timer, so an administrator disabling
    it or changing its permissions on another computer takes effect here too.

    Emits ``changed`` with the fresh ``User`` (or ``None`` once the account is
    gone) only when something that matters differs.  A failed query is ignored:
    an unreachable database is no reason to sign anyone out.
    """

    changed = pyqtSignal(object)

    INTERVAL_MS = 60_000

    def __init__(self, settings, user, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._user = user
        self._worker = None
        self._timer = QTimer(self)
        self._timer.setInterval(self.INTERVAL_MS)
        self._timer.timeout.connect(self.check_now)

    def start(self):
        self._timer.start()

    def stop(self):
        self._timer.stop()
        settle(self._worker)

    def set_user(self, user):
        self._user = user

    def check_now(self):
        if self._user is None or (self._worker is not None and self._worker.isRunning()):
            return
        worker = _AccountCheck(self.settings, self._user.uuid)
        worker.checked.connect(self._on_checked)
        self._worker = worker
        worker.start()

    def _on_checked(self, fresh, succeeded):
        if not succeeded or self._user is None:
            return
        if _signature(fresh) != _signature(self._user):
            if fresh is not None:
                self._user = fresh
            self.changed.emit(fresh)
