"""Compact metric tile with an optional tone colour."""
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
        self.set_tone("idle")

    def set_value(self, value, tone=None):
        self.value_label.setText(str(value))
        if tone is not None:
            self.set_tone(tone)

    def set_hint(self, hint):
        self.hint_label.setText(str(hint))

    def set_tone(self, tone):
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
