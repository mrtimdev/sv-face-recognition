"""A titled surface used by every screen.

``Card`` owns a header row (icon, title, subtitle and a right-hand action slot),
an optional divider and a body layout.  Screens add their widgets to
``card.body`` and their buttons to ``card.actions``.
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..icons import IconLabel
from ..theme import palette, stat_tile_colors


class Card(QFrame):
    def __init__(self, title="", subtitle="", icon="", tone="blue", theme="light",
                 divider=True, compact=False, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._theme = theme
        outer = QVBoxLayout(self)
        margin = 12 if compact else 16
        outer.setContentsMargins(margin, 12 if compact else 14, margin, margin)
        outer.setSpacing(10 if compact else 12)

        self.header = QWidget()
        self.header.setObjectName("cardHeader")
        header_row = QHBoxLayout(self.header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(10)

        self.icon = None
        if icon:
            self.icon = IconLabel(icon, size=20)
            header_row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)

        self.text_col = QVBoxLayout()
        self.text_col.setSpacing(1)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("cardTitle")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("cardSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setVisible(bool(subtitle))
        self.text_col.addWidget(self.title_label)
        self.text_col.addWidget(self.subtitle_label)
        header_row.addLayout(self.text_col)
        header_row.addStretch(1)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        header_row.addLayout(self.actions)
        outer.addWidget(self.header)

        self.divider = QFrame()
        self.divider.setObjectName("cardDivider")
        self.divider.setFixedHeight(1)
        self.divider.setVisible(bool(divider and title))
        outer.addWidget(self.divider)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)

        self.set_tone(tone)
        self.set_theme(theme)

    # --- helpers ----------------------------------------------------------
    def set_title(self, text):
        self.title_label.setText(text)

    def set_subtitle(self, text):
        self.subtitle_label.setText(text)
        self.subtitle_label.setVisible(bool(text))

    def set_tone(self, tone):
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)

    def add(self, widget, stretch=0):
        self.body.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout, stretch=0):
        self.body.addLayout(layout, stretch)
        return layout

    def set_theme(self, theme):
        self._theme = theme
        if self.icon is not None:
            colors = palette(theme)
            self.icon.set_icon_color(colors["muted"])
        for widget in (self.title_label, self.subtitle_label):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
