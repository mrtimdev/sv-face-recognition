"""Compact metric tile: tinted icon tile, label, value and hint line."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from ..icons import IconLabel
from ..theme import stat_tile_colors


class IconTile(QFrame):
    """Rounded square holding a single vector glyph."""

    def __init__(self, name="bolt", tone="blue", size=40, theme="light", parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.icon = IconLabel(name, size=int(size * 0.46))
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignCenter)
        self.set_tone(tone, theme)

    def set_tone(self, tone, theme):
        foreground, background = stat_tile_colors(theme, tone)
        self.setStyleSheet(
            f"QFrame {{ background-color: {background}; border-radius: 12px; }}")
        self.icon.set_icon_color(foreground)

    def set_icon(self, name):
        self.icon.set_icon(name)


class StatCard(QFrame):
    def __init__(self, title, value="-", hint="", icon="", tone="blue",
                 icon_color="", theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("statCard")
        self._tone = tone
        self._theme = theme

        outer = QHBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(12)

        self.icon_tile = None
        if icon:
            self.icon_tile = IconTile(icon, tone=tone, theme=theme)
            outer.addWidget(self.icon_tile)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
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

        self.setMinimumWidth(150)
        self._current_value = str(value)
        self._current_tone = "idle"
        self._current_hint = hint
        self.set_tone("idle")

    # --- api ---------------------------------------------------------------
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

    def set_icon(self, name):
        if self.icon_tile is not None:
            self.icon_tile.set_icon(name)

    def set_icon_tone(self, tone):
        self._tone = tone
        if self.icon_tile is not None:
            self.icon_tile.set_tone(tone, self._theme)

    def set_theme(self, theme):
        self._theme = theme
        if self.icon_tile is not None:
            self.icon_tile.set_tone(self._tone, theme)
        for widget in (self.title_label, self.value_label, self.hint_label):
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    @staticmethod
    def _repolish(widget, tone):
        widget.setProperty("tone", tone)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
