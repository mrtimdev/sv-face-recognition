"""Dropdown panel anchored under a header button.

A frameless ``Qt.Popup`` window with a translucent margin, so the rounded card
can carry a soft shadow; it slides and fades in and closes on an outside
click or Escape like a native menu.  The shadow is painted in layers rather
than with ``QGraphicsDropShadowEffect``, which would re-render every child
through an offscreen pixmap.
"""
import time

from PyQt6.QtCore import (QEasingCurve, QParallelAnimationGroup, QPoint,
                          QPropertyAnimation, QRectF, Qt, pyqtSignal)
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import QFrame, QVBoxLayout, QWidget

# Clicking the anchor while the panel is open first closes the popup; ignore
# the same click re-opening it immediately.
REOPEN_GUARD_SEC = 0.25


class PopupPanel(QWidget):
    closed = pyqtSignal()

    MARGIN = 18

    def __init__(self, width=360, theme="light", parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName("plain")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._theme = theme
        self.closed_at = 0.0
        self.anchor = None
        outer = QVBoxLayout(self)
        margin = self.MARGIN
        outer.setContentsMargins(margin, margin - 10, margin, margin + 8)
        self.card = QFrame(self)
        self.card.setObjectName("popupCard")
        self.card.setFixedWidth(width)
        self._shadow = QColor(15, 23, 42)
        self._shadow_alpha = 4
        outer.addWidget(self.card)
        self.body = QVBoxLayout(self.card)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        self._intro = QParallelAnimationGroup(self)
        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(190)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(160)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._intro.addAnimation(self._slide)
        self._intro.addAnimation(self._fade)
        self.set_theme(theme)

    def recently_closed(self):
        return time.monotonic() - self.closed_at < REOPEN_GUARD_SEC

    def open_below(self, anchor):
        """Show the card right-aligned under *anchor*, kept on screen."""
        self.anchor = anchor
        self.adjustSize()
        margin = self.MARGIN
        corner = anchor.mapToGlobal(QPoint(anchor.width(), anchor.height()))
        x = corner.x() - self.width() + margin
        y = corner.y() + 10 - (margin - 10)
        screen = anchor.screen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() - margin + 6, min(x, area.right() - self.width() + margin - 6))
        target = QPoint(x, y)
        self._intro.stop()
        self._slide.setStartValue(target + QPoint(0, -8))
        self._slide.setEndValue(target)
        self.move(target + QPoint(0, -8))
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._intro.start()

    def hideEvent(self, event):
        self.closed_at = time.monotonic()
        super().hideEvent(event)
        self.closed.emit()

    def set_theme(self, theme):
        self._theme = theme
        self._shadow = QColor(0, 0, 0) if theme == "dark" else QColor(15, 23, 42)
        self._shadow_alpha = 10 if theme == "dark" else 4
        self.update()

    def paintEvent(self, event):
        """Soft drop shadow: stacked rounded rects whose alpha accumulates near the card."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        card = QRectF(self.card.geometry()).translated(0, 6)
        color = QColor(self._shadow)
        color.setAlpha(self._shadow_alpha)
        painter.setBrush(color)
        for step in range(1, 13):
            spread = step * 1.5
            painter.drawRoundedRect(card.adjusted(-spread, -spread * 0.5, spread, spread),
                                    16 + spread, 16 + spread)
        painter.end()
