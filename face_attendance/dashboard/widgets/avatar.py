"""Circular initials avatar.

Used by the header, the activity feed and the employee tables.  The colour is
derived from the name so the same person keeps the same badge everywhere.
"""
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath
from PyQt6.QtWidgets import QWidget

from ..theme import palette

PALETTE_ORDER = ("primary", "success", "warn", "danger", "online")


def initials(name, limit=2):
    parts = [part for part in str(name or "?").replace("_", " ").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:limit].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _tint(hex_color, factor):
    """Blend *hex_color* towards white (factor > 1) or black (factor < 1)."""
    color = QColor(hex_color)
    if factor >= 1:
        ratio = min(1.0, factor - 1.0)
        r, g, b = (int(channel + (255 - channel) * ratio) for channel in
                   (color.red(), color.green(), color.blue()))
    else:
        ratio = max(0.0, 1.0 - factor)
        r, g, b = (int(channel * (1 - ratio)) for channel in
                   (color.red(), color.green(), color.blue()))
    return QColor(r, g, b)


class Avatar(QWidget):
    def __init__(self, name="?", size=36, theme="light", parent=None):
        super().__init__(parent)
        self._name = name
        self._size = size
        self._theme = theme
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_name(self, name):
        name = name or "?"
        if name != self._name:
            self._name = name
            self.update()

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def _seed(self):
        return sum(ord(character) for character in str(self._name))

    def paintEvent(self, event):
        colors = palette(self._theme)
        base = QColor(colors[PALETTE_ORDER[self._seed() % len(PALETTE_ORDER)]])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, self._size, self._size))
        painter.setClipPath(path)
        painter.fillRect(0, 0, self._size, self._size, _tint(base.name(), 0.82))
        painter.setPen(_tint(base.name(), 0.55))
        font = QFont(self.font())
        font.setPixelSize(max(9, int(self._size * 0.38)))
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, self._size, self._size),
                         Qt.AlignmentFlag.AlignCenter, initials(self._name))
        painter.end()
