"""Small status chip: camera state, engine state, storage health."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel


class StatusPill(QFrame):
    def __init__(self, text="IDLE", tone="idle", parent=None):
        super().__init__(parent)
        self.setObjectName("pill")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 3, 10, 3)
        layout.setSpacing(6)
        self.label = QLabel(text)
        self.label.setObjectName("pillText")
        layout.addWidget(self.label)
        self.set_size_policy()
        self.set_status(text, tone)

    def set_size_policy(self):
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(), self.sizePolicy().verticalPolicy())
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def set_status(self, text, tone="idle"):
        self.label.setText(str(text))
        self.setProperty("tone", tone)
        self.label.style().unpolish(self.label)
        self.label.style().polish(self.label)
        self.style().unpolish(self)
        self.style().polish(self)
