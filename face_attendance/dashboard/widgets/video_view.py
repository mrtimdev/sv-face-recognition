"""Aspect-preserving video canvas.

The engine draws the brackets and animations into the frame before it arrives,
so this widget only letter-boxes the newest image and shows a message while no
frame is available.

Performance: QImage creation (which copies data for thread safety) happens in
``set_frame``, but the heavier QPixmap conversion is deferred to ``paintEvent``
so frames superseded between paint cycles never pay that cost.
"""
from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt6.QtWidgets import QSizePolicy, QWidget

from ..bridge import to_qimage
from ..theme import palette


class VideoView(QWidget):
    def __init__(self, parent=None, theme="dark", message="No camera feed",
                 subtitle="Start the engine to see the live preview"):
        super().__init__(parent)
        self.theme = theme
        self._pixmap = None
        self._pending_qimage = None
        self._title = message
        self._subtitle = subtitle
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)

    def set_theme(self, theme):
        self.theme = theme
        self.update()

    def set_frame(self, frame):
        """Store the newest frame as a QImage (thread-safe copy); pixmap conversion
        is deferred to paintEvent so superseded frames skip the GPU upload."""
        qimage = to_qimage(frame)
        if qimage.isNull():
            return
        self._pending_qimage = qimage
        self.update()

    def show_message(self, title, subtitle=""):
        self._pixmap = None
        self._pending_qimage = None
        self._title, self._subtitle = title, subtitle
        self.update()

    def clear_frame(self):
        self._pixmap = None
        self._pending_qimage = None
        self.update()

    def paintEvent(self, event):
        # Convert pending QImage to QPixmap only when we actually paint.
        if self._pending_qimage is not None:
            self._pixmap = QPixmap.fromImage(self._pending_qimage)
            self._pending_qimage = None

        colors = palette(self.theme)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(colors["video_bg"]))
        if self._pixmap is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(self._target(self._pixmap.size()), self._pixmap)
        else:
            self._draw_message(painter, colors)
        painter.end()

    def _target(self, size):
        scaled = size.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - scaled.width()) // 2,
                     (self.height() - scaled.height()) // 2,
                     scaled.width(), scaled.height())

    def _draw_message(self, painter, colors):
        painter.setPen(QColor(colors["muted"]))
        font = QFont(self.font())
        font.setPointSize(max(10, int(self.height() / 34)))
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(QRect(0, self.height() // 2 - 36, self.width(), 36),
                         Qt.AlignmentFlag.AlignCenter, self._title)
        if self._subtitle:
            font.setPointSize(max(9, int(self.height() / 52)))
            font.setWeight(QFont.Weight.Normal)
            painter.setFont(font)
            painter.drawText(QRect(0, self.height() // 2, self.width(), 36),
                             Qt.AlignmentFlag.AlignCenter, self._subtitle)
