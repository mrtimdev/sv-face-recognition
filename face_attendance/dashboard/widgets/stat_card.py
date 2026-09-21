"""Compact metric tile with an optional icon circle and tone colour."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout


class StatCard(QFrame):
    def __init__(self, title, value="-", hint="", icon="", icon_color="", parent=None):
        super().__init__(parent)
        self.setObjectName("statCard")
        outer = QHBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(14)

        self._icon_label = None
        if icon:
            self._icon_label = QLabel(icon)
            self._icon_label.setObjectName("statIcon")
            self._icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._icon_label.setFixedSize(44, 44)
            if icon_color:
                self._icon_label.setStyleSheet(
                    f"background-color: {icon_color}20; color: {icon_color}; "
                    f"border-radius: 10px; font-size: 20px;")
            outer.addWidget(self._icon_label)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("statTitle")
        self.value_label = QLabel(str(value))
        self.value_label.setObjectName("statValue")
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("statSubtext")
        self.hint_label.setWordWrap(True)
        for widget in (self.title_label, self.value_label, self.hint_label):
            text_col.addWidget(widget)
        outer.addLayout(text_col, 1)

        self.setMinimumWidth(140)
        self._current_value = str(value)
        self._current_tone = "idle"
        self._current_hint = hint
        self.set_tone("idle")

    def set_value(self, value, tone=None):
        text = str(value)
        if text != self._current_value:
            self._current_value = text
            self.value_label.setText(text)
        if tone is not None and tone != self._current_tone:
            self.set_tone(tone)

    def set_hint(self, hint):
        hint = str(hint)
        if hint != self._current_hint:
            self._current_hint = hint
            self.hint_label.setText(hint)

    def set_tone(self, tone):
        if tone != self._current_tone:
            self._current_tone = tone
            self._repolish(self.value_label, tone)

    def set_theme(self, theme):
        for widget in (self.title_label, self.value_label, self.hint_label):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    @staticmethod
    def _repolish(widget, tone):
        widget.setProperty("tone", tone)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
