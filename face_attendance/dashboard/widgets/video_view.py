"""Aspect-preserving video canvas.

The engine draws its own brackets and animations into the frame before it
arrives, so this widget only letter-boxes the newest image.  While no frame is
available it paints the "position your face in the frame" guide from the
design: dark canvas, green corner brackets and a dashed face silhouette.

Performance: QImage creation (which copies data for thread safety) happens in
``set_frame``, but the heavier QPixmap conversion is deferred to ``paintEvent``
so frames superseded between paint cycles never pay that cost.
"""
from PyQt6.QtCore import QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap)
from PyQt6.QtWidgets import QSizePolicy, QWidget

from ..bridge import to_qimage
from ..theme import palette

CHIP_BG = QColor(2, 6, 23, 184)
CHIP_FG = QColor(255, 255, 255, 235)


class VideoView(QWidget):
    def __init__(self, parent=None, theme="dark", message="No camera feed",
                 subtitle="Start the engine to see the live preview"):
        super().__init__(parent)
        self.theme = theme
        self._pixmap = None
        self._pending_qimage = None
        self._title = message
        self._subtitle = subtitle
        self._resolution = ""
        self._fps = None
        self._faces = None
        self._guide = True
        self._radius = 12
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAutoFillBackground(False)

    # --- inputs -----------------------------------------------------------
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

    def set_overlay(self, resolution=None, fps=None, faces=None):
        """Update the chips drawn on top of the canvas (None keeps the old value)."""
        changed = False
        if resolution is not None and resolution != self._resolution:
            self._resolution, changed = str(resolution), True
        if fps is not None and fps != self._fps:
            self._fps, changed = float(fps), True
        if faces is not None and faces != self._faces:
            self._faces, changed = int(faces), True
        if changed:
            self.update()

    def set_guide(self, enabled):
        self._guide = bool(enabled)
        self.update()

    def has_frame(self):
        return self._pixmap is not None or self._pending_qimage is not None

    # --- painting ---------------------------------------------------------
    def paintEvent(self, event):
        # Convert pending QImage to QPixmap only when we actually paint.
        if self._pending_qimage is not None:
            self._pixmap = QPixmap.fromImage(self._pending_qimage)
            self._pending_qimage = None

        colors = palette(self.theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        clip = QPainterPath()
        clip.addRoundedRect(bounds, self._radius, self._radius)
        painter.setClipPath(clip)
        painter.fillRect(self.rect(), self._canvas_gradient(colors))
        if self._pixmap is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(self._target(self._pixmap.size()), self._pixmap)
        elif self._guide:
            self._paint_guide(painter, colors)
        else:
            self._draw_message(painter, colors)
        self._paint_chips(painter)
        painter.end()

    def _canvas_gradient(self, colors):
        gradient = QLinearGradient(0, 0, 0, max(1, self.height()))
        gradient.setColorAt(0.0, QColor(colors["video_bg"]))
        gradient.setColorAt(1.0, QColor(colors["video_bg_2"]))
        return QBrush(gradient)

    def _target(self, size):
        scaled = size.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - scaled.width()) // 2,
                     (self.height() - scaled.height()) // 2,
                     scaled.width(), scaled.height())

    # --- idle guide -------------------------------------------------------
    def _paint_guide(self, painter, colors):
        """Corner brackets, a dashed face silhouette and the hint line.

        The art is laid out from the available height, so it stays balanced in a
        short panel (the live monitor) and a tall one (a full-screen preview).
        """
        width, height = self.width(), self.height()
        side = min(width, height)
        accent = QColor(colors["bracket"])

        pen = QPen(accent, max(2.0, side * 0.008))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        arm = max(14.0, min(side * 0.085, height * 0.15))
        inset_x = max(12.0, width * 0.055)
        inset_y = max(12.0, min(height * 0.11, side * 0.16))
        for corner_x, corner_y in ((inset_x, inset_y),
                                   (width - inset_x, inset_y),
                                   (inset_x, height - inset_y),
                                   (width - inset_x, height - inset_y)):
            sign_x = 1 if corner_x < width / 2 else -1
            sign_y = 1 if corner_y < height / 2 else -1
            path = QPainterPath(QPointF(corner_x, corner_y + sign_y * arm))
            path.lineTo(QPointF(corner_x, corner_y))
            path.lineTo(QPointF(corner_x + sign_x * arm, corner_y))
            painter.drawPath(path)

        # Dashed face silhouette inside the free area above the hint text
        title_size = max(12, int(height * 0.045))
        body_size = max(10, int(height * 0.032))
        text_block = title_size + (body_size + 4 if self._subtitle else 0) + 6
        chip_row = 44 if self._has_chips() else 0
        text_top = height - inset_y - chip_row - text_block
        top = inset_y + arm * 0.5
        free = max(24.0, text_top - top)
        box_w = min(width * 0.30, free * 0.85)
        box_h = free
        center_x = width / 2
        head_diameter = min(box_w * 0.58, box_h * 0.46)
        head_center = QPointF(center_x, top + head_diameter * 0.62)
        base_y = top + box_h * 0.98

        dashed = QPen(QColor(255, 255, 255, 150), max(1.4, side * 0.004))
        dashed.setStyle(Qt.PenStyle.DashLine)
        dashed.setDashPattern([3.0, 3.0])
        dashed.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(dashed)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        silhouette = QPainterPath()
        silhouette.addEllipse(head_center, head_diameter / 2, head_diameter / 2 * 1.12)
        painter.drawPath(silhouette)

        half = box_w / 2
        head_bottom = head_center.y() + head_diameter * 0.56
        neck_y = head_bottom + max(2.0, (base_y - head_bottom) * 0.30)
        shoulders = QPainterPath()
        shoulders.moveTo(center_x - half, base_y)
        shoulders.cubicTo(center_x - half * 1.06, neck_y + (base_y - neck_y) * 0.25,
                          center_x - half * 0.55, neck_y,
                          center_x, neck_y)
        shoulders.cubicTo(center_x + half * 0.55, neck_y,
                          center_x + half * 1.06, neck_y + (base_y - neck_y) * 0.25,
                          center_x + half, base_y)
        painter.drawPath(shoulders)

        # Hint text below the silhouette
        title_font = QFont(self.font())
        title_font.setPixelSize(title_size)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(255, 255, 255, 240))
        painter.drawText(QRectF(0, text_top, width, title_size + 4),
                         Qt.AlignmentFlag.AlignCenter, self._title)
        if self._subtitle:
            body_font = QFont(self.font())
            body_font.setPixelSize(body_size)
            painter.setFont(body_font)
            painter.setPen(QColor(255, 255, 255, 150))
            painter.drawText(QRectF(0, text_top + title_size + 4, width, body_size + 6),
                             Qt.AlignmentFlag.AlignCenter, self._subtitle)

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

    # --- overlay chips ----------------------------------------------------
    def _has_chips(self):
        return bool(self._resolution) or self._fps is not None or self._faces is not None

    def _paint_chips(self, painter):
        margin = 14
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        if self._faces is not None:
            label = f"{self._faces} face{'' if self._faces == 1 else 's'}"
            self._chip(painter, label, self.width() - margin, margin, align_right=True)
        bottom = self.height() - margin - 24
        cursor = margin
        if self._resolution:
            cursor += self._chip(painter, self._resolution, cursor, bottom) + 8
        if self._fps is not None:
            self._chip(painter, f"{self._fps:.1f} FPS", cursor, bottom)

    def _chip(self, painter, text, x, y, align_right=False):
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 20
        height = 24
        rect = QRectF(x - width if align_right else x, y, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(CHIP_BG)
        painter.drawRoundedRect(rect, 8, 8)
        painter.setPen(CHIP_FG)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        return int(width)

