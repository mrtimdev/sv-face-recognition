"""Access-denied notice: a dimmed scrim over the window with a centred card."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from ..icons import IconLabel
from ..theme import palette
from .overlay import WindowOverlay
from .stat_card import IconTile


class AccessDeniedDialog(WindowOverlay):
    """Says what was blocked, which permission it needs and who is signed in."""

    def __init__(self, action, permission_title, account="", theme="light", parent=None):
        super().__init__(theme, parent, dismissible=True, title="Access denied")
        c = palette(theme)
        column = self.column
        column.addWidget(IconTile("lock", tone="red", size=60, theme=theme), 0,
                         Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(16)
        title = QLabel("Access denied")
        title.setObjectName("dialogTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        column.addSpacing(6)
        self.message = QLabel(f"You don't have permission to {action}.")
        self.message.setObjectName("dialogSubtitle")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        column.addWidget(self.message)
        column.addSpacing(16)

        requirement = QFrame()
        requirement.setObjectName("requirementPill")
        # Qt ignores a border-radius larger than half the height; pin it for a capsule.
        requirement.setFixedHeight(30)
        pill = QHBoxLayout(requirement)
        pill.setContentsMargins(12, 6, 14, 6)
        pill.setSpacing(8)
        pill.addWidget(IconLabel("key", 15, c["danger"]))
        self.requirement = QLabel(f"Requires “{permission_title}”")
        self.requirement.setObjectName("requirementText")
        pill.addWidget(self.requirement)
        column.addWidget(requirement, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(14)

        self.account = QLabel(account)
        self.account.setObjectName("fieldHelp")
        self.account.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.account)
        if not account:
            self.account.hide()
        hint = QLabel("An administrator can grant it in Users & Access.")
        hint.setObjectName("fieldHelp")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(hint)
        column.addSpacing(20)
        self.ok_button = QPushButton("OK, got it")
        self.ok_button.setObjectName("primary")
        self.ok_button.setMinimumHeight(42)
        self.ok_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ok_button.setDefault(True)
        self.ok_button.clicked.connect(self.accept)
        column.addWidget(self.ok_button)

    def showEvent(self, event):
        super().showEvent(event)
        self.ok_button.setFocus()
