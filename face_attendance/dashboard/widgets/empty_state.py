"""Centred placeholder for empty tables, filters without matches, and so on."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout


class EmptyState(QFrame):
    def __init__(self, title="Nothing to show", body="", parent=None):
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(6)
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

    def set_message(self, title, body=""):
        self.title_label.setText(title)
        self.body_label.setText(body)