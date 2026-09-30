"""Screen title block: heading, subtitle and a right-hand action slot."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


class PageHeader(QWidget):
    def __init__(self, title="", subtitle="", parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        column = QVBoxLayout()
        column.setSpacing(2)
        # Only hide before parenting: showing a parentless label would briefly
        # create a top-level window.
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pageTitle")
        if not title:
            self.title_label.hide()
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("pageSubtitle")
        self.subtitle_label.setWordWrap(True)
        if not subtitle:
            self.subtitle_label.hide()
        column.addWidget(self.title_label)
        column.addWidget(self.subtitle_label)
        # The text column takes the free width; a trailing stretch would squeeze
        # the word-wrapped subtitle down to its minimum and break it mid-sentence.
        row.addLayout(column, 1)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(self.actions)

    def set_title(self, text):
        self.title_label.setText(text)
        self.title_label.setVisible(bool(text))

    def set_subtitle(self, text):
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))

    def add_action(self, widget):
        self.actions.addWidget(widget)
        return widget

    def add_action_layout(self, layout):
        self.actions.addLayout(layout)
        return layout
