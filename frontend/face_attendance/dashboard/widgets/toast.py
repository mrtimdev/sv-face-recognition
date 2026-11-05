"""One-line transient feedback bar with a tone icon."""
from PyQt6.QtCore import QSize, QTimer, Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from ..icons import IconLabel, make_icon
from ..theme import palette, tone_color

TONES = {"info": "bolt", "ok": "check-circle", "bad": "alert", "warn": "alert"}


class ToastBar(QFrame):
    def __init__(self, parent=None, duration_ms=6000, theme="light"):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setProperty("tone", "info")
        self._theme = theme
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(10)
        self.icon = IconLabel("bolt", size=18)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.label = QLabel("")
        self.label.setObjectName("toastText")
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self.close_button = QPushButton("")
        self.close_button.setObjectName("iconButtonFlat")
        self.close_button.setFixedSize(26, 26)
        self.close_button.setToolTip("Dismiss")
        self.close_button.clicked.connect(self.hide_message)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide_message)
        self.duration_ms = duration_ms
        self.set_theme(theme)
        self.hide()

    def show_message(self, text, tone="info", duration_ms=None):
        self.label.setText(str(text))
        self.setProperty("tone", tone)
        self.icon.set_icon(TONES.get(tone, "bolt"))
        self.icon.set_icon_color(tone_color(self._theme, tone))
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()
        self.timer.start(duration_ms or self.duration_ms)

    def set_theme(self, theme):
        self._theme = theme
        tone = self.property("tone") or "info"
        self.icon.set_icon_color(tone_color(theme, tone))
        colors = palette(theme)
        self.close_button.setIcon(make_icon("x", 12, colors["muted"]))
        self.close_button.setIconSize(QSize(12, 12))
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)

    def hide_message(self):
        self.timer.stop()
        self.hide()
