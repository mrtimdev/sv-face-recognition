"""Small status chip: camera state, engine state, storage health."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel

from ..theme import palette, tone_color

TONE_DOTS = {"ok": "●", "warn": "●", "bad": "●", "info": "●", "idle": "○"}


class StatusPill(QFrame):
    def __init__(self, text="IDLE", tone="idle", dot=False, theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("pill")
        self._theme = theme
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(6)
        self.dot_label = QLabel(TONE_DOTS.get(tone, "●"))
        self.dot_label.setFixedWidth(10)
        self.dot_label.setVisible(bool(dot))
        layout.addWidget(self.dot_label)
        self.label = QLabel(text)
        self.label.setObjectName("pillText")
        layout.addWidget(self.label)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.set_status(text, tone)

    def set_status(self, text, tone="idle"):
        self.label.setText(str(text))
        self.setProperty("tone", tone)
        self.dot_label.setStyleSheet(
            f"color: {tone_color(self._theme, tone)}; font-size: 10px; background: transparent;")
        self.dot_label.setText(TONE_DOTS.get(tone, "●"))
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)
        self.style().unpolish(self)
        self.style().polish(self)
