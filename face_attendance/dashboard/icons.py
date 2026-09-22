"""Vector line icons painted with QPainter.

The dashboard ships as a single bundle, so instead of image assets every glyph
is described as a few primitives on a 24x24 grid and stroked with the current
pen.  ``make_icon`` returns a QIcon for widgets, ``IconLabel`` is a ready made
label and ``apply_button_icon`` decorates a QPushButton.
"""
import math

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QLabel, QPushButton

GRID = 24.0


def _pen(color, width=1.8):
    pen = QPen(QColor(color), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


# ---------------------------------------------------------------------------
#  Glyph painters (all draw inside a 24x24 box)
# ---------------------------------------------------------------------------

def _monitor(p, pen):
    p.setPen(pen)
    p.drawRoundedRect(QRectF(2.5, 4.5, 19, 12.5), 2.4, 2.4)
    p.drawLine(QPointF(8.5, 21), QPointF(15.5, 21))
    p.drawLine(QPointF(12, 17), QPointF(12, 21))


def _list(p, pen):
    p.setPen(pen)
    for y in (7.0, 12.0, 17.0):
        p.drawLine(QPointF(9.5, y), QPointF(20.5, y))
        p.drawEllipse(QPointF(4.6, y), 1.4, 1.4)


def _chart(p, pen):
    p.setPen(pen)
    p.drawLine(QPointF(3.5, 20.5), QPointF(20.5, 20.5))
    for x, top in ((7.0, 13.5), (12.0, 8.5), (17.0, 11.0)):
        p.drawLine(QPointF(x, 20.5), QPointF(x, top))


def _users(p, pen):
    p.setPen(pen)
    p.drawEllipse(QRectF(8.4, 3.6, 7.2, 7.2))
    path = QPainterPath()
    path.moveTo(4.2, 20.4)
    path.arcMoveTo(QRectF(4.2, 11.8, 15.6, 20.0), 180)
    path.arcTo(QRectF(4.2, 11.8, 15.6, 20.0), 180, -180)
    p.drawPath(path)
    p.drawEllipse(QRectF(2.4, 6.0, 5.0, 5.0))
    path = QPainterPath()
    path.arcMoveTo(QRectF(0.6, 10.6, 8.0, 10.0), 180)
    path.arcTo(QRectF(0.6, 10.6, 8.0, 10.0), 180, -140)
    p.drawPath(path)


def _gear(p, pen):
    p.setPen(pen)
    p.drawEllipse(QPointF(12.0, 12.0), 3.1, 3.1)
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        p.drawLine(QPointF(12 + 6.6 * math.cos(rad), 12 + 6.6 * math.sin(rad)),
                   QPointF(12 + 9.3 * math.cos(rad), 12 + 9.3 * math.sin(rad)))
    p.drawEllipse(QPointF(12.0, 12.0), 8.2, 8.2)


def _sliders(p, pen):
    p.setPen(pen)
    for y, knob in ((7.5, 15.5), (12.0, 9.0), (16.5, 13.0)):
        p.drawLine(QPointF(3.5, y), QPointF(20.5, y))
        p.drawEllipse(QPointF(knob, y), 1.9, 1.9)


def _bell(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(5.8, 17.0)
    path.lineTo(5.8, 11.0)
    path.arcMoveTo(QRectF(5.8, 4.0, 12.4, 12.4), 180)
    path.arcTo(QRectF(5.8, 4.0, 12.4, 12.4), 180, -180)
    path.lineTo(18.2, 17.0)
    p.drawPath(path)
    p.drawLine(QPointF(3.8, 17.0), QPointF(20.2, 17.0))
    p.drawArc(QRectF(9.6, 18.2, 4.8, 3.6), 180 * 16, 180 * 16)


def _calendar(p, pen):
    p.setPen(pen)
    p.drawRoundedRect(QRectF(3.5, 5.5, 17, 15), 2.4, 2.4)
    p.drawLine(QPointF(3.5, 10.2), QPointF(20.5, 10.2))
    p.drawLine(QPointF(8.0, 3.4), QPointF(8.0, 6.6))
    p.drawLine(QPointF(16.0, 3.4), QPointF(16.0, 6.6))
    p.drawLine(QPointF(7.6, 14.4), QPointF(9.4, 14.4))
    p.drawLine(QPointF(14.6, 14.4), QPointF(16.4, 14.4))

def _chevron(p, pen, rotation):
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(9.0, 5.6)
    path.lineTo(15.4, 12.0)
    path.lineTo(9.0, 18.4)
    p.save()
    p.translate(12.0, 12.0)
    p.rotate(rotation)
    p.translate(-12.0, -12.0)
    p.drawPath(path)
    p.restore()


def _play(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(7.6, 4.6)
    path.lineTo(19.4, 12.0)
    path.lineTo(7.6, 19.4)
    path.closeSubpath()
    p.drawPath(path)


def _pause(p, pen):
    p.setPen(pen)
    p.drawLine(QPointF(9.0, 5.0), QPointF(9.0, 19.0))
    p.drawLine(QPointF(15.0, 5.0), QPointF(15.0, 19.0))


def _stop(p, pen):
    p.setPen(pen)
    p.drawRoundedRect(QRectF(6.0, 6.0, 12.0, 12.0), 2.4, 2.4)


def _restart(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.arcMoveTo(QRectF(5.2, 5.2, 13.6, 13.6), 40)
    path.arcTo(QRectF(5.2, 5.2, 13.6, 13.6), 40, 290)
    p.drawPath(path)
    p.drawLine(QPointF(19.0, 5.2), QPointF(19.0, 11.4))
    p.drawLine(QPointF(19.0, 11.4), QPointF(12.8, 11.4))


def _refresh(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.arcMoveTo(QRectF(4.8, 4.8, 14.4, 14.4), 160)
    path.arcTo(QRectF(4.8, 4.8, 14.4, 14.4), 160, 250)
    p.drawPath(path)
    p.drawLine(QPointF(4.8, 6.4), QPointF(4.8, 12.0))
    p.drawLine(QPointF(4.8, 12.0), QPointF(10.2, 12.0))


def _camera(p, pen):
    p.setPen(pen)
    p.drawRoundedRect(QRectF(2.6, 6.4, 18.8, 13.0), 3.0, 3.0)
    p.drawEllipse(QPointF(12.0, 12.9), 3.6, 3.6)
    path = QPainterPath()
    path.moveTo(8.6, 6.4)
    path.lineTo(9.8, 3.8)
    path.lineTo(14.2, 3.8)
    path.lineTo(15.4, 6.4)
    p.drawPath(path)


def _image(p, pen):
    p.setPen(pen)
    p.drawRoundedRect(QRectF(3.0, 4.4, 18.0, 15.2), 2.6, 2.6)
    p.drawEllipse(QPointF(8.6, 9.6), 1.7, 1.7)
    path = QPainterPath()
    path.moveTo(4.2, 17.6)
    path.lineTo(9.6, 12.6)
    path.lineTo(14.0, 16.0)
    path.lineTo(17.0, 13.6)
    path.lineTo(19.8, 16.2)
    p.drawPath(path)


def _folder(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(3.0, 7.2)
    path.lineTo(3.0, 19.0)
    path.lineTo(21.0, 19.0)
    path.lineTo(21.0, 7.2)
    path.lineTo(11.6, 7.2)
    path.lineTo(9.6, 4.4)
    path.lineTo(3.0, 4.4)
    path.closeSubpath()
    p.drawPath(path)


def _expand(p, pen):
    p.setPen(pen)
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        x = 12 + 8.4 * sx
        y = 12 + 8.4 * sy
        p.drawLine(QPointF(x, y), QPointF(x - 4.6 * sx, y))
        p.drawLine(QPointF(x, y), QPointF(x, y - 4.6 * sy))


def _more(p, pen):
    color = pen.color()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for x in (6.4, 12.0, 17.6):
        p.drawEllipse(QPointF(x, 12.0), 1.5, 1.5)
def _download(p, pen):
    p.setPen(pen)
    p.drawLine(QPointF(12.0, 3.6), QPointF(12.0, 14.8))
    p.drawLine(QPointF(7.4, 10.2), QPointF(12.0, 14.8))
    p.drawLine(QPointF(16.6, 10.2), QPointF(12.0, 14.8))
    p.drawLine(QPointF(4.4, 19.8), QPointF(19.6, 19.8))


def _upload(p, pen):
    p.setPen(pen)
    p.drawLine(QPointF(12.0, 20.4), QPointF(12.0, 9.4))
    p.drawLine(QPointF(7.4, 14.0), QPointF(12.0, 9.4))
    p.drawLine(QPointF(16.6, 14.0), QPointF(12.0, 9.4))
    p.drawLine(QPointF(4.4, 4.6), QPointF(19.6, 4.6))


def _search(p, pen):
    p.setPen(pen)
    p.drawEllipse(QPointF(10.6, 10.6), 6.0, 6.0)
    p.drawLine(QPointF(15.0, 15.0), QPointF(20.2, 20.2))


def _check(p, pen):
    p.setPen(pen)
    p.drawLine(QPointF(5.0, 12.6), QPointF(10.0, 17.4))
    p.drawLine(QPointF(10.0, 17.4), QPointF(19.2, 6.6))


def _check_circle(p, pen):
    p.setPen(pen)
    p.drawEllipse(QPointF(12.0, 12.0), 8.6, 8.6)
    p.drawLine(QPointF(7.8, 12.4), QPointF(10.9, 15.4))
    p.drawLine(QPointF(10.9, 15.4), QPointF(16.4, 9.0))


def _x(p, pen):
    p.setPen(pen)
    p.drawLine(QPointF(6.6, 6.6), QPointF(17.4, 17.4))
    p.drawLine(QPointF(17.4, 6.6), QPointF(6.6, 17.4))


def _alert(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(12.0, 3.8)
    path.lineTo(21.0, 19.6)
    path.lineTo(3.0, 19.6)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(12.0, 9.6), QPointF(12.0, 14.2))
    p.drawLine(QPointF(12.0, 17.0), QPointF(12.0, 17.1))


def _shield(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(12.0, 3.2)
    path.lineTo(19.8, 6.4)
    path.lineTo(19.8, 12.6)
    path.quadTo(19.8, 18.4, 12.0, 20.8)
    path.quadTo(4.2, 18.4, 4.2, 12.6)
    path.lineTo(4.2, 6.4)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(8.8, 12.0), QPointF(11.4, 14.6))
    p.drawLine(QPointF(11.4, 14.6), QPointF(15.6, 9.8))


def _database(p, pen):
    p.setPen(pen)
    p.drawEllipse(QRectF(3.6, 3.4, 16.8, 5.6))
    p.drawLine(QPointF(3.6, 6.2), QPointF(3.6, 17.8))
    p.drawLine(QPointF(20.4, 6.2), QPointF(20.4, 17.8))
    path = QPainterPath()
    path.arcMoveTo(QRectF(3.6, 10.2, 16.8, 5.6), 180)
    path.arcTo(QRectF(3.6, 10.2, 16.8, 5.6), 180, 180)
    p.drawPath(path)
    path = QPainterPath()
    path.arcMoveTo(QRectF(3.6, 15.0, 16.8, 5.6), 180)
    path.arcTo(QRectF(3.6, 15.0, 16.8, 5.6), 180, 180)
    p.drawPath(path)


def _user_plus(p, pen):
    p.setPen(pen)
    p.drawEllipse(QRectF(3.8, 3.6, 8.0, 8.0))
    path = QPainterPath()
    path.arcMoveTo(QRectF(1.6, 12.4, 12.4, 12.4), 180)
    path.arcTo(QRectF(1.6, 12.4, 12.4, 12.4), 180, -180)
    p.drawPath(path)
    p.drawLine(QPointF(18.6, 8.8), QPointF(18.6, 15.6))
    p.drawLine(QPointF(15.2, 12.2), QPointF(22.0, 12.2))


def _trash(p, pen):
    p.setPen(pen)
    p.drawLine(QPointF(4.0, 6.6), QPointF(20.0, 6.6))
    p.drawLine(QPointF(9.4, 6.6), QPointF(9.4, 4.2))
    p.drawLine(QPointF(14.6, 6.6), QPointF(14.6, 4.2))
    p.drawLine(QPointF(9.4, 4.2), QPointF(14.6, 4.2))
    path = QPainterPath()
    path.moveTo(6.2, 6.6)
    path.lineTo(7.4, 20.4)
    path.lineTo(16.6, 20.4)
    path.lineTo(17.8, 6.6)
    p.drawPath(path)

def _clock(p, pen):
    p.setPen(pen)
    p.drawEllipse(QPointF(12.0, 12.0), 8.8, 8.8)
    p.drawLine(QPointF(12.0, 6.8), QPointF(12.0, 12.2))
    p.drawLine(QPointF(12.0, 12.2), QPointF(16.2, 14.6))


def _send(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(21.0, 3.6)
    path.lineTo(2.8, 11.0)
    path.lineTo(10.6, 13.6)
    path.lineTo(13.4, 21.2)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(10.6, 13.6), QPointF(21.0, 3.6))


def _bolt(p, pen):
    p.setPen(pen)
    path = QPainterPath()
    path.moveTo(13.6, 2.6)
    path.lineTo(5.6, 13.8)
    path.lineTo(11.4, 13.8)
    path.lineTo(10.4, 21.4)
    path.lineTo(18.4, 10.2)
    path.lineTo(12.6, 10.2)
    path.closeSubpath()
    p.drawPath(path)


def _face_id(p, pen):
    p.setPen(pen)
    for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        x = 12 + 9.0 * sx
        y = 12 + 9.0 * sy
        p.drawLine(QPointF(x, y), QPointF(x - 4.8 * sx, y))
        p.drawLine(QPointF(x, y), QPointF(x, y - 4.8 * sy))
    p.drawLine(QPointF(9.0, 10.2), QPointF(9.0, 12.4))
    p.drawLine(QPointF(15.0, 10.2), QPointF(15.0, 12.4))
    path = QPainterPath()
    path.moveTo(8.6, 15.2)
    path.quadTo(12.0, 18.2, 15.4, 15.2)
    p.drawPath(path)


def _user(p, pen):
    p.setPen(pen)
    p.drawEllipse(QRectF(7.6, 3.8, 8.8, 8.8))
    path = QPainterPath()
    path.arcMoveTo(QRectF(3.4, 12.2, 17.2, 17.2), 180)
    path.arcTo(QRectF(3.4, 12.2, 17.2, 17.2), 180, -180)
    p.drawPath(path)


def _grid(p, pen):
    p.setPen(pen)
    for x, y in ((3.4, 3.4), (13.4, 3.4), (3.4, 13.4), (13.4, 13.4)):
        p.drawRoundedRect(QRectF(x, y, 7.2, 7.2), 1.8, 1.8)


def _dot(p, pen):
    color = pen.color()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(12.0, 12.0), 5.0, 5.0)
    p.setBrush(Qt.BrushStyle.NoBrush)


_GLYPHS = {
    "monitor": _monitor,
    "list": _list,
    "chart": _chart,
    "users": _users,
    "user": _user,
    "user-plus": _user_plus,
    "grid": _grid,
    "dot": _dot,
    "gear": _gear,
    "settings": _gear,
    "sliders": _sliders,
    "bell": _bell,
    "calendar": _calendar,
    "chevron-down": lambda p, pen: _chevron(p, pen, 90),
    "chevron-up": lambda p, pen: _chevron(p, pen, -90),
    "chevron-right": lambda p, pen: _chevron(p, pen, 0),
    "chevron-left": lambda p, pen: _chevron(p, pen, 180),
    "play": _play,
    "pause": _pause,
    "stop": _stop,
    "restart": _restart,
    "refresh": _refresh,
    "camera": _camera,
    "image": _image,
    "folder": _folder,
    "expand": _expand,
    "more": _more,
    "download": _download,
    "export": _download,
    "upload": _upload,
    "search": _search,
    "check": _check,
    "check-circle": _check_circle,
    "x": _x,
    "alert": _alert,
    "shield": _shield,
    "database": _database,
    "trash": _trash,
    "clock": _clock,
    "send": _send,
    "bolt": _bolt,
    "face-id": _face_id,
}


def available(name):
    return name in _GLYPHS


def make_pixmap(name, size=20, color="#64748B", stroke=1.8, ratio=1.0):
    """Rasterise *name* into a square QPixmap (device pixel ratio aware)."""
    painter_fn = _GLYPHS.get(name, _dot)
    scale = max(1.0, float(ratio))
    pixmap = QPixmap(int(size * scale), int(size * scale))
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / GRID, size / GRID)
    painter_fn(painter, _pen(color, stroke))
    painter.end()
    return pixmap


def make_icon(name, size=20, color="#64748B", stroke=1.8, ratio=1.0):
    return QIcon(make_pixmap(name, size, color, stroke, ratio))


class IconLabel(QLabel):
    """A label that paints a single vector glyph in a theme-aware colour."""

    def __init__(self, name, size=20, color="#64748B", stroke=1.8, parent=None):
        super().__init__(parent)
        self._name = name
        self._size = size
        self._color = color
        self._stroke = stroke
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._refresh()

    def set_icon_color(self, color):
        if color != self._color:
            self._color = color
            self._refresh()

    def set_icon(self, name):
        self._name = name
        self._refresh()

    def set_icon_size(self, size):
        self._size = size
        self.setFixedSize(size, size)
        self._refresh()

    def _refresh(self):
        ratio = self.devicePixelRatioF()
        self.setPixmap(make_pixmap(self._name, self._size, self._color, self._stroke, ratio))


def apply_button_icon(button, name, color=None, size=16):
    """Give a QPushButton a leading vector glyph (keeps any existing text)."""
    if not isinstance(button, QPushButton):
        return
    tone = color or button.palette().color(button.foregroundRole()).name()
    button.setIcon(make_icon(name, size, tone))
    button.setIconSize(QSize(size, size))

