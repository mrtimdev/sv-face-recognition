"""Signed-in user chip for the header and its account dropdown."""
from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ..icons import IconLabel, make_icon
from ..theme import palette
from .avatar import Avatar
from .form import repolish
from .popup import PopupPanel

ROLE_TITLES = {"admin": "Administrator", "user": "Standard user"}
NAME_WIDTH = 150


def role_title(role):
    return ROLE_TITLES.get(str(role or "").lower(), str(role or "User").title())


class UserChip(QPushButton):
    """Avatar with presence dot, name and role; sized to its content."""

    def __init__(self, name="Admin", subtitle="", image=None, theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("userChip")
        self.setFixedHeight(44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme = theme
        row = QHBoxLayout(self)
        row.setContentsMargins(5, 5, 10, 5)
        row.setSpacing(10)
        self.avatar = Avatar(name, size=34, theme=theme, image=image, status="online")
        row.addWidget(self.avatar)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.name_label = QLabel("")
        self.name_label.setObjectName("userChipName")
        self.role_label = QLabel("")
        self.role_label.setObjectName("userChipRole")
        text.addWidget(self.name_label)
        text.addWidget(self.role_label)
        row.addLayout(text)
        self.chevron = IconLabel("chevron-down", 14)
        row.addWidget(self.chevron)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.set_user(name, subtitle, image)
        self.set_theme(theme)

    def set_user(self, name, subtitle="", image=None):
        name = name or "Admin"
        metrics = self.name_label.fontMetrics()
        self.name_label.setText(metrics.elidedText(name, Qt.TextElideMode.ElideRight, NAME_WIDTH))
        self.role_label.setText(subtitle)
        self.role_label.setVisible(bool(subtitle))
        self.avatar.set_name(name)
        self.avatar.set_image(image)
        self.setToolTip(f"Signed in as {name}" + (f" • {subtitle}" if subtitle else ""))
        self.setAccessibleName(f"Account menu for {name}")
        self.updateGeometry()

    def set_open(self, is_open):
        repolish(self, "open", bool(is_open))
        self.chevron.set_icon("chevron-up" if is_open else "chevron-down")

    def set_theme(self, theme):
        self._theme = theme
        self.avatar.set_theme(theme)
        self.chevron.set_icon_color(palette(theme)["muted"])

    def sizeHint(self):
        hint = self.layout().sizeHint()
        return QSize(max(120, hint.width()), 44)

    def minimumSizeHint(self):
        return self.sizeHint()


class UserMenuPanel(PopupPanel):
    """Account dropdown: identity card, shortcuts and sign out."""

    profileRequested = pyqtSignal()
    usersRequested = pyqtSignal()
    settingsRequested = pyqtSignal()
    signOutRequested = pyqtSignal()

    def __init__(self, theme="light", parent=None):
        super().__init__(width=296, theme=theme, parent=parent)
        identity = QWidget()
        identity.setObjectName("plain")
        head = QHBoxLayout(identity)
        head.setContentsMargins(16, 16, 16, 14)
        head.setSpacing(12)
        self.avatar = Avatar("?", size=52, theme=theme, status="online")
        head.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name_label = QLabel("")
        self.name_label.setObjectName("popupName")
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("popupMeta")
        self.role_pill = QLabel("")
        self.role_pill.setObjectName("rolePill")
        self.role_pill.setFixedHeight(20)
        text.addWidget(self.name_label)
        text.addWidget(self.meta_label)
        text.addSpacing(4)
        text.addWidget(self.role_pill, 0, Qt.AlignmentFlag.AlignLeft)
        head.addLayout(text, 1)
        self.body.addWidget(identity)
        self.body.addWidget(self._divider())

        items = QVBoxLayout()
        items.setContentsMargins(8, 8, 8, 8)
        items.setSpacing(2)
        self.profile_item = self._item("user", "My profile", self.profileRequested)
        self.users_item = self._item("shield", "Users && access", self.usersRequested)
        self.settings_item = self._item("gear", "Settings", self.settingsRequested)
        for item in (self.profile_item, self.users_item, self.settings_item):
            items.addWidget(item)
        self.body.addLayout(items)
        self.sign_out_divider = self._divider()
        self.body.addWidget(self.sign_out_divider)
        bottom = QVBoxLayout()
        bottom.setContentsMargins(8, 8, 8, 8)
        self.sign_out_item = self._item("log-out", "Sign out", self.signOutRequested, danger=True)
        self._signals = {self.profile_item: self.profileRequested,
                         self.users_item: self.usersRequested,
                         self.settings_item: self.settingsRequested,
                         self.sign_out_item: self.signOutRequested}
        bottom.addWidget(self.sign_out_item)
        self.body.addLayout(bottom)
        self.set_theme(theme)

    def set_user(self, name, meta="", role="", image=None, can_sign_out=True, has_profile=True):
        name = name or "Admin"
        self.avatar.set_name(name)
        self.avatar.set_image(image)
        metrics = self.name_label.fontMetrics()
        self.name_label.setText(metrics.elidedText(name, Qt.TextElideMode.ElideRight, 196))
        self.meta_label.setText(self.meta_label.fontMetrics().elidedText(
            meta, Qt.TextElideMode.ElideRight, 196))
        self.meta_label.setToolTip(meta)
        self.meta_label.setVisible(bool(meta))
        self.role_pill.setText(role.upper() if role else "")
        self.role_pill.setVisible(bool(role))
        repolish(self.role_pill, "role", "admin" if str(role).lower() in ("admin", "administrator") else "user")
        self.profile_item.setVisible(has_profile)
        self.sign_out_item.setVisible(can_sign_out)
        self.sign_out_divider.setVisible(can_sign_out)
        self.card.adjustSize()
        self.adjustSize()

    def set_shortcuts(self, users=True, settings=True):
        """Only offer shortcuts to screens the signed-in account may open."""
        self.users_item.setVisible(users)
        self.settings_item.setVisible(settings)
        self.card.adjustSize()
        self.adjustSize()

    def set_theme(self, theme):
        super().set_theme(theme)
        if not hasattr(self, "avatar"):
            return
        c = palette(theme)
        self.avatar.set_theme(theme)
        for item, glyph, color in ((self.profile_item, "user", c["text_secondary"]),
                                   (self.users_item, "shield", c["text_secondary"]),
                                   (self.settings_item, "gear", c["text_secondary"]),
                                   (self.sign_out_item, "log-out", c["danger"])):
            item.setIcon(make_icon(glyph, 17, color, ratio=2.0))

    def _divider(self):
        line = QFrame()
        line.setObjectName("hLine")
        line.setFixedHeight(1)
        return line

    def _item(self, glyph, text, signal, danger=False):
        button = QPushButton(f"  {text}")
        button.setObjectName("popupItemDanger" if danger else "popupItem")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIconSize(QSize(17, 17))
        button.setMinimumHeight(38)
        button.clicked.connect(self._activate)
        return button

    def _activate(self):
        signal = self._signals.get(self.sender())
        self.close()
        if signal is not None:
            # Let the popup finish closing before a modal dialog takes over.
            QTimer.singleShot(0, signal.emit)
