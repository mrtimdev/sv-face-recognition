"""One-line transient feedback bar."""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel


class ToastBar(QFrame):
    def __init__(self, parent=None, duration_ms=6000):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setProperty("tone", "info")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide_message)
        self.duration_ms = duration_ms
        self.hide()

    def show_message(self, text, tone="info", duration_ms=None):
        self.label.setText(str(text))
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)
        self.show()
        self.timer.start(duration_ms or self.duration_ms)

    def hide_message(self):
        self.timer.stop()
        self.hide()
