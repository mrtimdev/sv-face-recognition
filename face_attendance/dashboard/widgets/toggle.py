"""Animated pill switch (used for "Auto scroll" and boolean settings rows).

Qt has no switch widget, so the pill is painted directly: the knob travels on a
``QVariantAnimation`` and the track colour follows the checked state.
"""
from PyQt6.QtCore import QEasingCurve, QRectF, Qt, QVariantAnimation, pyqtProperty
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QAbstractButton

from ..theme import palette


class ToggleSwitch(QAbstractButton):
    def __init__(self, checked=False, theme="light", parent=None):
        super().__init__(parent)
        self._theme = theme
        self._progress = 1.0 if checked else 0.0
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(44, 24)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.valueChanged.connect(self._on_animation)
        self.toggled.connect(self._animate)

    # --- painting state ---------------------------------------------------
    def _on_animation(self, value):
        self._progress = float(value)
        self.update()

    def _animate(self, checked):
        self._animation.stop()
        self._animation.setStartValue(self._progress)
        self._animation.setEndValue(1.0 if checked else 0.0)
        self._animation.start()
        self.update()

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def sizeHint(self):
        from PyQt6.QtCore import QSize
        return QSize(44, 24)

    def paintEvent(self, event):
        colors = palette(self._theme)
        off = QColor(colors["border"])
        on = QColor(colors["primary"])
        progress = self._progress
        track = QColor(
            int(off.red() + (on.red() - off.red()) * progress),
            int(off.green() + (on.green() - off.green()) * progress),
            int(off.blue() + (on.blue() - off.blue()) * progress),
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(0, 0, self.width(), self.height()), 12, 12)
        diameter = self.height() - 6
        left = 3 + (self.width() - diameter - 6) * progress
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(QRectF(left, 3, diameter, diameter))
        painter.end()
