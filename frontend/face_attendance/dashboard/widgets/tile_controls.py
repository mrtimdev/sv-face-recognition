"""Small per-camera controls pinned to a monitor tile, plus a smooth size animator.

The controls sit dimmed in the tile's corner and brighten while the pointer is
over the tile, so they never hide the faces being recognized. Animating a
QSplitter's sizes gives tiles a smooth enlarge/back-to-grid transition without
re-rendering video through a graphics effect.
"""
from PyQt6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, QSize, Qt, QVariantAnimation, pyqtSignal
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QToolButton, QWidget

from ..icons import make_icon

REST_OPACITY = 0.55
FADE_MS = 180
RESIZE_MS = 260


class TileControls(QWidget):
    pauseClicked = pyqtSignal()
    snapshotClicked = pyqtSignal()
    reconnectClicked = pyqtSignal()
    enlargeClicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tileControls")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(2)
        self.pause = self._button("pause", "Pause recording from this camera", self.pauseClicked)
        self.snapshot = self._button("camera", "Save a snapshot of this camera", self.snapshotClicked)
        self.reconnect = self._button("restart", "Reconnect this camera", self.reconnectClicked)
        self.enlarge = self._button("expand", "Enlarge (or double-click the tile)", self.enlargeClicked)
        for button in (self.pause, self.snapshot, self.reconnect, self.enlarge):
            row.addWidget(button)
        self.setStyleSheet("""
            QWidget#tileControls { background: rgba(2,6,23,0.72); border-radius: 19px;
                                   border: 1px solid rgba(255,255,255,0.16); }
            QToolButton { background: transparent; border: none; border-radius: 15px; }
            QToolButton:hover { background: rgba(255,255,255,0.16); }
            QToolButton:pressed { background: rgba(255,255,255,0.28); }
            QToolButton:disabled { background: transparent; }
        """)
        self._paused = False
        self._enlarged = False
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(REST_OPACITY)
        self.setGraphicsEffect(self._effect)
        self._fade = QPropertyAnimation(self._effect, b"opacity", self)
        self._fade.setDuration(FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        if parent is not None:
            parent.installEventFilter(self)

    def _button(self, glyph, tooltip, signal):
        button = QToolButton(self)
        button.setIcon(make_icon(glyph, 18, "#FFFFFF"))
        button.setIconSize(QSize(18, 18))
        button.setFixedSize(30, 30)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(signal.emit)
        return button

    def set_paused(self, paused):
        self._paused = bool(paused)
        self.pause.setIcon(make_icon("play" if paused else "pause", 18, "#FFFFFF"))
        tip = "Resume recording from this camera" if paused else "Pause recording from this camera"
        self.pause.setToolTip(tip)
        self.pause.setAccessibleName(tip)
        if paused:
            self.fade_to(1.0)  # A paused camera keeps its controls in view.

    def set_enlarged(self, enlarged):
        self._enlarged = bool(enlarged)
        self.enlarge.setIcon(make_icon("x" if enlarged else "expand", 18, "#FFFFFF"))
        tip = "Back to the grid" if enlarged else "Enlarge (or double-click the tile)"
        self.enlarge.setToolTip(tip)
        self.enlarge.setAccessibleName(tip)

    def set_running(self, running):
        for button in (self.pause, self.snapshot, self.reconnect):
            button.setEnabled(bool(running))

    def fade_to(self, opacity):
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(float(opacity))
        self._fade.start()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Enter:
            self.fade_to(1.0)
        elif event.type() == QEvent.Type.Leave and not self._paused:
            self.fade_to(REST_OPACITY)
        return False


class SplitterAnimator(QObject):
    """Ease a QSplitter from its current sizes to new ones."""

    def __init__(self, splitter, parent=None):
        super().__init__(parent or splitter)
        self.splitter = splitter
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(RESIZE_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.valueChanged.connect(self._step)
        self._from, self._to = [], []

    def animate(self, weights, animate=True):
        """*weights* are relative shares, e.g. [1, 0] collapses the second widget."""
        total = sum(self.splitter.sizes())
        laid_out = total >= 50 and self.splitter.isVisible()
        if not laid_out:
            total = 10000  # Not shown yet: Qt scales these shares to the real size later.
        share = sum(weights) or 1
        target = [round(total * weight / share) for weight in weights]
        self._animation.stop()
        if not animate or not laid_out or len(target) != self.splitter.count():
            self.splitter.setSizes(target)
            return
        self._from, self._to = self.splitter.sizes(), target
        self._animation.start()

    def _step(self, progress):
        if len(self._from) != len(self._to):
            return
        self.splitter.setSizes([round(a + (b - a) * progress) for a, b in zip(self._from, self._to)])

    @property
    def running(self):
        return self._animation.state() == QVariantAnimation.State.Running
