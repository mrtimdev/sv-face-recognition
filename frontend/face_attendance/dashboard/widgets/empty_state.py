"""Centred placeholder for empty tables, filters without matches, and so on."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..icons import IconLabel
from ..theme import palette


class EmptyState(QFrame):
    def __init__(self, title="Nothing to show", body="", icon="search", theme="light",
                 parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        self._theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 26, 18, 26)
        layout.setSpacing(8)

        self.icon = IconLabel(icon, size=26)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("emptyTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label = QLabel(body)
        self.body_label.setObjectName("emptyBody")
        self.body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_label.setWordWrap(True)
        layout.addStretch(1)
        layout.addWidget(self.title_label)
        layout.addWidget(self.body_label)
        layout.addStretch(1)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(self.actions)
        self.set_theme(theme)

    def add_action(self, text, callback=None, object_name="primary"):
        button = QPushButton(text)
        if object_name:
            button.setObjectName(object_name)
        if callback is not None:
            button.clicked.connect(callback)
        self.actions.addWidget(button)
        return button

    def set_message(self, title, body=""):
        self.title_label.setText(title)
        self.body_label.setText(body)

    def set_icon(self, name):
        self.icon.set_icon(name)

    def set_theme(self, theme):
        self._theme = theme
        colors = palette(theme)
        self.icon.set_icon_color(colors["muted"])
        for widget in (self.title_label, self.body_label):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
