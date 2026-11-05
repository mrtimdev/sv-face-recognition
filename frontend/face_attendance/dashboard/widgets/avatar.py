"""Circular avatar: a profile photo when one exists, otherwise initials.

Used by the header, the activity feed and the employee tables.  The colour is
derived from the name so the same person keeps the same badge everywhere.  An
optional ring and presence dot are painted inside the widget bounds, so the
avatar never needs extra layout room.
"""
import os

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (QColor, QFont, QImageReader, QLinearGradient, QPainter, QPainterPath,
                         QPen, QPixmap)
from PyQt6.QtWidgets import QWidget

from ..icons import make_pixmap
from ..theme import palette

# Deliberately not the semantic tones: a red avatar should never read as an error.
# Each keeps white initials legible (at least 3:1 for the bold, large text).
AVATAR_HUES = ("#4F46E5", "#0284C7", "#059669", "#D97706", "#DB2777",
               "#7C3AED", "#0D9488", "#2563EB")


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


def load_avatar(path, size=256):
    """Decode *path* at avatar resolution; an unreadable file gives a null pixmap."""
    if not path or not os.path.isfile(str(path)):
        return QPixmap()
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    source = reader.size()
    if source.isValid() and min(source.width(), source.height()) > size:
        reader.setScaledSize(source.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding))
    image = reader.read()
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


class Avatar(QWidget):
    def __init__(self, name="?", size=36, theme="light", parent=None, image=None,
                 ring=False, status=None):
        super().__init__(parent)
        self._name = name
        self._size = size
        self._theme = theme
        self._pixmap = QPixmap()
        self._ring = ring
        self._status = status
        self._dimmed = False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.set_image(image)

    def set_name(self, name):
        name = name or "?"
        if name != self._name:
            self._name = name
            self.update()

    def set_image(self, image):
        """Accept a file path, a QPixmap or ``None`` (back to initials)."""
        if isinstance(image, QPixmap):
            self._pixmap = image
        else:
            self._pixmap = load_avatar(image, max(96, self._size * 3))
        self.update()

    def has_image(self):
        return not self._pixmap.isNull()

    def set_ring(self, ring):
        """``False``, ``True`` (card-coloured ring) or a colour string."""
        self._ring = ring
        self.update()

    def set_status(self, status):
        """Presence dot: ``None``, ``"online"`` or ``"away"``."""
        self._status = status
        self.update()

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def set_dimmed(self, dimmed):
        """Fade the avatar, e.g. for a disabled account."""
        self._dimmed = bool(dimmed)
        self.update()

    def _seed(self):
        return sum((index + 1) * ord(character) for index, character in enumerate(str(self._name)))

    def _placeholder(self):
        return str(self._name or "").strip() in ("", "?")

    def paintEvent(self, event):
        colors = palette(self._theme)
        size = float(self._size)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self._dimmed:
            painter.setOpacity(0.42)
        ring_width = max(2.0, size * 0.05) if self._ring else 0.0
        face = QRectF(ring_width, ring_width, size - 2 * ring_width, size - 2 * ring_width)

        if self._ring:
            ring_color = QColor(colors["card"] if self._ring is True else self._ring)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(ring_color)
            painter.drawEllipse(QRectF(0, 0, size, size))

        path = QPainterPath()
        path.addEllipse(face)
        painter.save()
        painter.setClipPath(path)
        if not self._pixmap.isNull():
            photo = self._pixmap.scaled(
                int(face.width() * self.devicePixelRatioF()),
                int(face.height() * self.devicePixelRatioF()),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            photo.setDevicePixelRatio(self.devicePixelRatioF())
            logical_w = photo.width() / photo.devicePixelRatio()
            logical_h = photo.height() / photo.devicePixelRatio()
            painter.drawPixmap(QRectF(face.x() + (face.width() - logical_w) / 2,
                                      face.y() + (face.height() - logical_h) / 2,
                                      logical_w, logical_h), photo, QRectF(photo.rect()))
        elif self._placeholder():
            painter.fillRect(face, QColor(colors["chip_bg"]))
            glyph_size = face.width() * 0.5
            glyph = make_pixmap("user", int(glyph_size), colors["muted"], 1.6,
                                ratio=self.devicePixelRatioF())
            painter.drawPixmap(QRectF(face.center().x() - glyph_size / 2,
                                      face.center().y() - glyph_size / 2,
                                      glyph_size, glyph_size), glyph, QRectF(glyph.rect()))
        else:
            base = QColor(AVATAR_HUES[self._seed() % len(AVATAR_HUES)])
            gradient = QLinearGradient(face.topLeft(), face.bottomRight())
            gradient.setColorAt(0.0, _tint(base.name(), 1.18))
            gradient.setColorAt(1.0, _tint(base.name(), 0.9))
            painter.fillRect(face, gradient)
            painter.setPen(QColor("#FFFFFF"))
            font = QFont(self.font())
            font.setPixelSize(max(9, int(face.width() * 0.38)))
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.drawText(face, Qt.AlignmentFlag.AlignCenter, initials(self._name))
        painter.restore()

        if self._status:
            dot = max(8.0, size * 0.28)
            rect = QRectF(size - dot, size - dot, dot, dot)
            painter.setPen(QPen(QColor(colors["card"]), max(1.5, dot * 0.2)))
            painter.setBrush(QColor(colors["online" if self._status == "online" else "warn"]))
            painter.drawEllipse(rect.adjusted(0.75, 0.75, -0.75, -0.75))
        painter.end()
