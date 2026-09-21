"""Compact metric tile with an optional tone colour.

Caches the current value and tone so redundant updates (which would trigger
expensive Qt style re-polishing) are skipped entirely.
"""
from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout


class StatCard(QFrame):
    def __init__(self, title, value="-", hint="", parent=None):
        super().__init__(parent)
        self.setObjectName("statCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(1)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("statTitle")
        self.value_label = QLabel(str(value))
        self.value_label.setObjectName("statValue")
        self.hint_label = QLabel(hint)
        self.hint_label.setObjectName("statHint")
        self.hint_label.setWordWrap(True)
        for widget in (self.title_label, self.value_label, self.hint_label):
            layout.addWidget(widget)
        self.setMinimumWidth(120)
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
