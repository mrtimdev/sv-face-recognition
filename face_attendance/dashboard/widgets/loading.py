"""Loading feedback: a smooth progress line and a button with an inline spinner.

Real work (a database connection, a password hash) rarely reports a
percentage, so ``ProgressLine`` eases to a milestone and then trickles towards
the next one until the caller advances, finishes or fails it.
"""
from PyQt6.QtCore import QEasingCurve, QRectF, QSize, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QPushButton, QSizePolicy, QWidget

from ..icons import make_icon, make_pixmap
from ..theme import palette
from .form import repolish

TONES = {"primary": "primary", "ok": "success", "bad": "danger", "warn": "warn"}


class ProgressLine(QWidget):
    """Thin bar that eases between targets, trickles while waiting, then fades."""

    completed = pyqtSignal()

    def __init__(self, theme="light", height=3, parent=None, rounded=False):
        super().__init__(parent)
        self._rounded = rounded
        self.setObjectName("plain")
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._theme = theme
        self._value = 0.0
        self._opacity = 0.0
        self._phase = 0.0
        self._tone = "primary"
        self._creep_to = None
        self._creeping = False
        self._finishing = False

        self._motion = QVariantAnimation(self)
        self._motion.valueChanged.connect(self._on_value)
        self._motion.finished.connect(self._on_motion_done)
        self._fade = QVariantAnimation(self)
        self._fade.valueChanged.connect(self._on_opacity)
        self._fade.finished.connect(self._faded)
        self._shimmer = QVariantAnimation(self)
        self._shimmer.setStartValue(0.0)
        self._shimmer.setEndValue(1.0)
        self._shimmer.setDuration(1300)
        self._shimmer.setLoopCount(-1)
        self._shimmer.valueChanged.connect(self._on_phase)
        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.timeout.connect(self._fade_out)

    # --- api ---------------------------------------------------------------
    def value(self):
        return self._value

    def tone(self):
        return self._tone

    def is_active(self):
        return self._opacity > 0.0 and not self._finishing

    def start(self, target=0.3, duration=520):
        self._stop_all()
        self._value = 0.0
        self._tone = "primary"
        self._finishing = False
        self._on_opacity(1.0)
        self._shimmer.start()
        self.advance(target, duration)

    def advance(self, target, duration=480, creep_to=None):
        """Ease to *target*, then creep towards *creep_to* until told otherwise."""
        if self._finishing:
            return
        target = max(self._value, min(0.97, float(target)))
        self._creep_to = min(0.95, creep_to if creep_to is not None else target + 0.16)
        self._creeping = False
        self._animate(target, duration, QEasingCurve.Type.OutCubic)

    def finish(self, tone="ok", duration=380):
        """Run to 100% in *tone*, hold briefly, fade out and emit ``completed``."""
        self._finishing = True
        self._creep_to = None
        self._tone = tone
        self._animate(1.0, duration, QEasingCurve.Type.InOutCubic)

    def fail(self):
        """Keep the current length, turn red, then fade away."""
        self._finishing = True
        self._creep_to = None
        self._motion.stop()
        self._tone = "bad"
        self.update()
        self._hold.start(650)

    def reset(self):
        self._stop_all()
        self._value = 0.0
        self._finishing = False
        self._on_opacity(0.0)

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    # --- animation plumbing ----------------------------------------------
    def _stop_all(self):
        self._motion.stop()
        self._fade.stop()
        self._hold.stop()
        self._shimmer.stop()

    def _animate(self, target, duration, curve):
        self._motion.stop()
        self._motion.setStartValue(float(self._value))
        self._motion.setEndValue(float(target))
        self._motion.setDuration(max(1, int(duration)))
        self._motion.setEasingCurve(curve)
        self._motion.start()

    def _on_motion_done(self):
        if self._finishing:
            if self._value >= 0.999:
                self._hold.start(260)
            return
        if not self._creeping and self._creep_to is not None and self._creep_to > self._value:
            # A slow, decelerating creep reads as "still working" without lying about it.
            self._creeping = True
            self._animate(self._creep_to, 6000, QEasingCurve.Type.OutQuad)

    def _fade_out(self):
        self._fade.stop()
        self._fade.setStartValue(float(self._opacity))
        self._fade.setEndValue(0.0)
        self._fade.setDuration(420)
        self._fade.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._fade.start()

    def _faded(self):
        self._shimmer.stop()
        self._finishing = False
        self._value = 0.0
        self.completed.emit()

    def _on_value(self, value):
        self._value = float(value)
        self.update()

    def _on_opacity(self, value):
        self._opacity = float(value)
        self.update()

    def _on_phase(self, value):
        self._phase = float(value)
        if self._opacity > 0:
            self.update()

    # --- painting -----------------------------------------------------------
    def paintEvent(self, event):
        if self._opacity <= 0.0:
            return
        c = palette(self._theme)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(self._opacity)
        painter.setPen(Qt.PenStyle.NoPen)
        height = float(self.height())
        radius = height / 2 if self._rounded else 0.0
        track = QColor(c["border_soft"])
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 0, self.width(), height), radius, radius)

        width = self.width() * self._value
        if width > 0.5:
            color = QColor(c[TONES.get(self._tone, "primary")])
            glow = QColor(color).lighter(135)
            gradient = QLinearGradient(0, 0, max(1.0, width), 0)
            gradient.setColorAt(0.0, glow)
            gradient.setColorAt(1.0, color)
            painter.setBrush(gradient)
            painter.drawRoundedRect(QRectF(0, 0, width, height), height / 2, height / 2)
            if self._tone == "primary":
                band = 90.0
                x = -band + (width + band) * self._phase
                shine = QLinearGradient(x, 0, x + band, 0)
                shine.setColorAt(0.0, QColor(255, 255, 255, 0))
                shine.setColorAt(0.5, QColor(255, 255, 255, 150))
                shine.setColorAt(1.0, QColor(255, 255, 255, 0))
                painter.setBrush(shine)
                painter.drawRoundedRect(QRectF(0, 0, width, height), radius, radius)
        painter.end()


def spinner_pixmap(angle, size=16, color="#FFFFFF", ratio=2.0):
    """One frame of a circular spinner: faint track plus a 100 degree arc."""
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    stroke = max(1.6, size * 0.13)
    rect = QRectF(stroke, stroke, size - 2 * stroke, size - 2 * stroke)
    track = QColor(color)
    track.setAlphaF(0.3)
    pen = QPen(track, stroke)
    painter.setPen(pen)
    painter.drawEllipse(rect)
    pen = QPen(QColor(color), stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawArc(rect, int(-angle * 16), int(100 * 16))
    painter.end()
    return pixmap


class LoadingButton(QPushButton):
    """Push button that swaps its glyph for a spinner while work is running.

    The spinner is also registered for the disabled icon mode, so disabling the
    button during work does not grey the animation out.
    """

    def __init__(self, text="", icon=None, icon_color="#FFFFFF", trailing_icon=False, parent=None):
        super().__init__(text, parent)
        self._idle_text = text
        self._icon_name = icon
        self._icon_color = icon_color
        self._trailing = trailing_icon
        self._loading = False
        self.setIconSize(QSize(16, 16))
        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(820)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(self._draw_spinner)
        self._show_idle()

    def is_loading(self):
        return self._loading

    def set_idle(self, text=None, icon=None, icon_color=None):
        if text is not None:
            self._idle_text = text
        if icon is not None:
            self._icon_name = icon
        if icon_color is not None:
            self._icon_color = icon_color
        if not self._loading and self.property("state") != "ok":
            self._show_idle()

    def start_loading(self, text):
        self._loading = True
        repolish(self, "state", None)
        repolish(self, "loading", True)
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.setText(text)
        self.setEnabled(False)
        self._draw_spinner(0.0)
        self._spin.start()

    def set_success(self, text):
        self._loading = False
        self._spin.stop()
        repolish(self, "loading", False)
        repolish(self, "state", "ok")
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.setText(text)
        self._set_both_modes(make_pixmap("check", 16, "#FFFFFF", 2.4, ratio=2.0))
        self.setEnabled(False)

    def stop_loading(self):
        self._loading = False
        self._spin.stop()
        repolish(self, "loading", False)
        repolish(self, "state", None)
        self.setEnabled(True)
        self._show_idle()

    def _show_idle(self):
        self.setText(self._idle_text)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft if self._trailing
                                else Qt.LayoutDirection.LeftToRight)
        if self._icon_name:
            self.setIcon(make_icon(self._icon_name, 16, self._icon_color, 2.0, ratio=2.0))
        else:
            self.setIcon(QIcon())

    def _draw_spinner(self, angle):
        self._set_both_modes(spinner_pixmap(float(angle), 16, self._icon_color))

    def _set_both_modes(self, pixmap):
        icon = QIcon()
        icon.addPixmap(pixmap, QIcon.Mode.Normal)
        icon.addPixmap(pixmap, QIcon.Mode.Disabled)
        self.setIcon(icon)
