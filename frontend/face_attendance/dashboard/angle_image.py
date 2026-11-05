"""The picture on an angle card, with animated changes.

A new image rises and sharpens into place over the old one while a band of light crosses it, a ring
in the result's color spreads out, and a badge pops in. While an image is being generated, a beam
scans the card. Nothing replays when the same image is set again, so a refresh that changed nothing
stays still, and nothing animates while the card is off screen.
"""
from PyQt6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QTimer, QVariantAnimation
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QLabel, QWidget

from .. import angle_checks

ACCENT = "#3B82F6"
MATCHED_COLOR = "#22C55E"
WARNING_COLOR = angle_checks.WARNING_COLOR
CHANGE_MS = 620
RING_MS = 760
SCAN_MS = 1500
CORNER = 8
HEAD_BIAS = 0.2  # How much of a tall photo's extra height is cut from its top; the rest goes from the bottom.

MARK_MATCHED, MARK_WARNING = "matched", "warning"


class ScoreBar(QWidget):
    """Verified share in green and warning share in amber, sliding to each new value."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self._shown = (0.0, 0.0)
        self._start = (0.0, 0.0)
        self._target = (0.0, 0.0)
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(CHANGE_MS)
        self._animation.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutCubic))
        self._animation.valueChanged.connect(self._on_value)

    def set_score(self, verified_percent, warning_percent):
        target = (verified_percent / 100, warning_percent / 100)
        if target == self._target:
            return
        self._target = target
        if not self.isVisible():
            self._shown = target
            self.update()
            return
        self._start = self._shown
        self._animation.stop()
        self._animation.start()

    def _on_value(self, value):
        t = float(value)
        self._shown = tuple(a + (b - a) * t for a, b in zip(self._start, self._target))
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        area = QRectF(self.rect())
        radius = area.height() / 2
        clip = QPainterPath()
        clip.addRoundedRect(area, radius, radius)
        painter.setClipPath(clip)
        track = QColor("#94A3B8")
        track.setAlpha(50)
        painter.fillRect(area, track)
        verified, warning = self._shown
        painter.fillRect(QRectF(0, 0, area.width() * verified, area.height()), QColor(MATCHED_COLOR))
        painter.fillRect(QRectF(area.width() * (1 - warning), 0, area.width() * warning, area.height()),
                         QColor(WARNING_COLOR))
        painter.end()


class AngleImage(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._outgoing = None
        self._changing = False
        self._change = 1.0
        self._ring = 1.0
        self._scan = None
        self._mark = ""
        self.next_delay_ms = 0  # So cards changing together can follow one another.
        self._change_animation = self._animation(CHANGE_MS, QEasingCurve.Type.OutCubic, self._on_change)
        self._change_animation.finished.connect(self._change_finished)
        self._ring_animation = self._animation(RING_MS, QEasingCurve.Type.OutQuad, self._on_ring)
        self._scan_animation = self._animation(SCAN_MS, QEasingCurve.Type.InOutSine, self._on_scan)
        self._scan_animation.setLoopCount(-1)
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.timeout.connect(self._begin)

    def _animation(self, duration, easing, slot):
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(duration)
        animation.setEasingCurve(QEasingCurve(easing))
        animation.valueChanged.connect(slot)
        return animation

    def _on_change(self, value):
        self._change = float(value)
        self.update()

    def _on_ring(self, value):
        self._ring = float(value)
        self.update()

    def _on_scan(self, value):
        self._scan = float(value)
        self.update()

    def _change_finished(self):
        self._changing = False
        self._outgoing = None
        self.update()

    # --- what changes ------------------------------------------------------
    @property
    def animating(self):
        return (self._changing or self._delay.isActive()
                or self._ring_animation.state() == QVariantAnimation.State.Running)

    @property
    def scanning(self):
        return self._scan_animation.state() == QVariantAnimation.State.Running

    @property
    def mark(self):
        return self._mark

    def _keep_outgoing(self):
        current = self.pixmap()
        if current is not None and not current.isNull():
            self._outgoing = QPixmap(current)

    def clear(self):
        self._keep_outgoing()
        super().clear()

    def setText(self, text):
        self._keep_outgoing()
        super().setText(text)
        if text:
            self._play()

    def setPixmap(self, pixmap):
        self._keep_outgoing()
        previous = self._outgoing
        super().setPixmap(pixmap)
        if previous is not None and previous.toImage() == pixmap.toImage():
            self._settle()
            self.next_delay_ms = 0
            return
        self._play()

    def set_mark(self, mark):
        """The badge: MARK_MATCHED, MARK_WARNING or "". A new one pulses the ring."""
        if mark == self._mark:
            return
        self._mark = mark
        if mark and self.isVisible() and not self._changing:
            self._ring_animation.stop()
            self._ring_animation.start()
        self.update()

    def start_scan(self):
        if self.isVisible():
            self._scan_animation.start()

    def stop_scan(self):
        self._scan_animation.stop()
        self._scan = None
        self.update()

    def _settle(self):
        self._delay.stop()
        self._change_animation.stop()
        self._changing = False
        self._outgoing = None
        self._change = 1.0
        self.update()

    def _play(self):
        if not self.isVisible():
            self._settle()
            self.next_delay_ms = 0
            return
        self._delay.stop()
        self._change_animation.stop()
        self._changing = True
        self._change = 0.0
        if self.next_delay_ms > 0:
            self._delay.start(self.next_delay_ms)
            self.next_delay_ms = 0
        else:
            self._begin()
        self.update()

    def _begin(self):
        self._change_animation.start()
        self._ring_animation.stop()
        self._ring_animation.start()

    # --- painting ----------------------------------------------------------
    def paintEvent(self, event):
        current = self.pixmap()
        has_image = current is not None and not current.isNull()
        if not has_image:
            super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        area = QRectF(self.rect())
        if self._changing:
            self._paint_change(painter, area, current if has_image else None)
        elif has_image:
            self._paint_still(painter, area, current)
        if self._scan is not None:
            self._paint_scan(painter, area)
        if has_image and self._mark == MARK_WARNING:
            painter.setPen(QPen(QColor(WARNING_COLOR), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(area.adjusted(1, 1, -1, -1), CORNER, CORNER)
        if self._ring < 1.0 and (has_image or self._outgoing is not None):
            self._paint_ring(painter, area)
        if has_image and self._mark and not (self._changing and self._change < 0.45):
            self._paint_badge(painter, area)
        painter.end()

    @staticmethod
    def _draw_scaled(painter, area, pixmap, scale, rise, opacity):
        # Fills the card; the clip trims the rest. A portrait keeps its top, where the head is.
        fit = max(area.width() / pixmap.width(), area.height() / pixmap.height()) * scale
        width, height = pixmap.width() * fit, pixmap.height() * fit
        spare = height - area.height() * scale
        top = area.center().y() - area.height() * scale / 2 - spare * HEAD_BIAS
        target = QRectF(area.center().x() - width / 2, top + rise, width, height)
        painter.setOpacity(opacity)
        painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))
        painter.setOpacity(1.0)

    def _paint_still(self, painter, area, pixmap):
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(area, CORNER, CORNER)
        painter.setClipPath(clip)
        self._draw_scaled(painter, area, pixmap, 1.0, 0, 1.0)
        painter.restore()

    def _paint_change(self, painter, area, incoming):
        t = self._change
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(area, CORNER, CORNER)
        painter.setClipPath(clip)
        if self._outgoing is not None:
            self._draw_scaled(painter, area, self._outgoing, 1 + 0.08 * t, -10 * t, 1 - t)
        if incoming is not None:
            self._draw_scaled(painter, area, incoming, 1.14 - 0.14 * t, 14 * (1 - t), t)
            self._paint_sheen(painter, area, t)
        painter.restore()

    @staticmethod
    def _paint_sheen(painter, area, t):
        """A band of light crossing the new image."""
        travel = area.width() * 1.8
        start = -area.width() * 0.4 + travel * t
        gradient = QLinearGradient(QPointF(start, 0), QPointF(start + area.width() * 0.4, area.height() * 0.4))
        light = QColor("#FFFFFF")
        for stop, alpha in ((0.0, 0), (0.5, 120), (1.0, 0)):
            light.setAlpha(round(alpha * (1 - t)))
            gradient.setColorAt(stop, light)
        painter.fillRect(area, gradient)

    def _paint_scan(self, painter, area):
        painter.save()
        clip = QPainterPath()
        clip.addRoundedRect(area, CORNER, CORNER)
        painter.setClipPath(clip)
        color = QColor(ACCENT)
        y = area.top() + area.height() * self._scan
        length = area.height() * 0.3
        trail = QLinearGradient(QPointF(0, y), QPointF(0, y - length))
        for stop, alpha in ((0.0, 110), (1.0, 0)):
            color.setAlpha(alpha)
            trail.setColorAt(stop, color)
        painter.fillRect(QRectF(area.left(), y - length, area.width(), length), trail)
        color.setAlpha(235)
        painter.setPen(QPen(color, 2))
        painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
        painter.restore()

    def _mark_color(self):
        return QColor({MARK_MATCHED: MATCHED_COLOR, MARK_WARNING: WARNING_COLOR}.get(self._mark, ACCENT))

    def _paint_ring(self, painter, area):
        t = self._ring
        color = self._mark_color()
        color.setAlpha(round(230 * (1 - t)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, 2 + 5 * (1 - t)))
        inset = 2 + 2 * (1 - t)
        painter.drawRoundedRect(area.adjusted(inset, inset, -inset, -inset), CORNER, CORNER)

    def _paint_badge(self, painter, area):
        # Grows from nothing, overshoots a little as the ring goes out, then settles.
        over = min(1.0, self._ring * 2.2) - 1
        scale = 1 + 2.7 * over ** 3 + 1.7 * over ** 2
        if scale < 0.05:
            return
        radius = 11 * scale
        center = QPointF(area.right() - 15, area.top() + 15)
        painter.setPen(QPen(QColor("#FFFFFF"), 1.5))
        painter.setBrush(self._mark_color())
        painter.drawEllipse(center, radius, radius)
        font = QFont(self.font())
        font.setBold(True)
        font.setPixelSize(max(9, round(13 * scale)))
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        box = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "✓" if self._mark == MARK_MATCHED else "!")
