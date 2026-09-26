"""A single nonblocking capture pulse, painted above the dashboard widgets."""
from PyQt6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QPointF, QRectF, Qt, QVariantAnimation
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRadialGradient
from PyQt6.QtWidgets import QWidget


class CaptureFlash(QWidget):
    DURATION_MS = 720

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._progress = 0.0
        self._preview = False
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(self.DURATION_MS)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.Linear)
        self.animation.valueChanged.connect(self._advance)
        self.animation.finished.connect(self.hide)
        parent.installEventFilter(self)
        self.hide()

    def trigger(self, preview=False):
        # Coalesce simultaneous employees into one pulse instead of strobing.
        if self.animation.state() == QAbstractAnimation.State.Running:
            return
        self._preview = bool(preview)
        self._progress = 0.0
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.animation.start()

    def cancel(self):
        self.animation.stop()
        self.hide()

    def _advance(self, value):
        self._progress = float(value)
        self.update()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget():
            if event.type() == QEvent.Type.Resize:
                self.setGeometry(self.parentWidget().rect())
            elif event.type() == QEvent.Type.Hide:
                self.cancel()
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        progress = self._progress
        fade = (1.0 - progress) ** 2
        ease = 1.0 - (1.0 - progress) ** 3
        width, height = self.width(), self.height()
        side = min(width, height)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # One restrained exposure pulse, fading independently of camera FPS.
            flash = max(0.0, 1.0 - progress / .38) ** 2
            painter.fillRect(self.rect(), QColor(238, 251, 255, round(62 * flash)))
            glow = QRadialGradient(QPointF(width / 2, height / 2), max(width, height) * .68)
            glow.setColorAt(0.0, QColor(45, 212, 255, 0))
            glow.setColorAt(.65, QColor(45, 212, 255, round(12 * fade)))
            glow.setColorAt(1.0, QColor(0, 110, 255, round(72 * fade)))
            painter.fillRect(self.rect(), glow)

            inset = 18 + side * .065 * (1 - ease)
            arm = min(85.0, side * .12)
            path = QPainterPath()
            for x, y, dx, dy in ((inset, inset, 1, 1),
                                  (width-inset, inset, -1, 1),
                                  (inset, height-inset, 1, -1),
                                  (width-inset, height-inset, -1, -1)):
                path.moveTo(x, y + dy * arm)
                path.lineTo(x, y)
                path.lineTo(x + dx * arm, y)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for thickness, alpha in ((12, 20), (6, 50), (2, 235)):
                pen = QPen(QColor(72, 222, 255, round(alpha * fade)), thickness)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                painter.setPen(pen)
                painter.drawPath(path)

            # The badge describes capture, never an uncommitted database save.
            opacity = min(1.0, max(0.0, (1 - progress) / .30))
            painter.setOpacity(opacity)
            badge = QRectF(width / 2 - 112, height * .80 + 8 * ease, 224, 46)
            painter.setPen(QPen(QColor(97, 226, 255, 180), 1))
            painter.setBrush(QColor(8, 29, 51, 235))
            painter.drawRoundedRect(badge, 23, 23)
            painter.setPen(QPen(QColor(106, 236, 255), 1.8))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            camera = QRectF(badge.left() + 24, badge.top() + 16, 19, 14)
            painter.drawRoundedRect(camera, 3, 3)
            painter.drawEllipse(camera.center(), 3.5, 3.5)
            painter.drawLine(QPointF(camera.left() + 5, camera.top() - 3),
                             QPointF(camera.right() - 5, camera.top() - 3))
            font = QFont(self.font())
            font.setPixelSize(12)
            font.setWeight(QFont.Weight.DemiBold)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.5)
            painter.setFont(font)
            painter.setPen(QColor(234, 251, 255))
            painter.drawText(badge.adjusted(46, 0, -10, 0), Qt.AlignmentFlag.AlignCenter,
                             "FLASH PREVIEW" if self._preview else "CAPTURED")
        finally:
            painter.end()
