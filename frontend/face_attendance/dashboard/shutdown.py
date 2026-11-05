"""Graceful quit and restart: stop everything behind a progress card.

Stopping the engine joins its threads, closes the camera and commits any
attendance still queued; Telegram and the system monitor wait for their own
threads.  All of that blocks, so it runs on a worker thread while the card
animates, and only afterwards does the application quit or start its
replacement - by then the engine lock is released, so the new instance can
take it.
"""
import logging
import sys

from PyQt6.QtCore import QObject, QRectF, Qt, QThread, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .icons import make_pixmap
from .theme import palette, stat_tile_colors
from .threads import settle
from .widgets.form import repolish
from .widgets.loading import ProgressLine, spinner_pixmap
from .widgets.overlay import WindowOverlay

STAGES = ("capture", "saved", "services")
TARGETS = {"capture": 0.38, "saved": 0.64}
STEP_LABELS = ("Stopping camera and face recognition", "Saving pending attendance",
               "Closing background services")
STEP_MIN_MS = 220
FINAL_HOLD_MS = 380
FINISH_BEAT_MS = 220
SLOW_AFTER_MS = 6000

# mode -> (title, subtitle, glyph, tone, final step, closing title)
MODES = {
    "restart": ("Restarting Face ID Attendance", "The application will restart immediately.",
                "restart", "blue", "Restarting the application", "Restarting now…"),
    "signout": ("Signing out", "The application will restart at the sign-in screen.",
                "log-out", "blue", "Returning to the sign-in screen", "Signing out now…"),
    "quit": ("Shutting down", "The application is shutting down. Please wait a moment.",
             "power", "red", "Closing the application", "Closing now…"),
    "update": ("Installing update", "SV Face ID will close, install the update and reopen.",
               "download", "blue", "Starting the installer", "Installing now…"),
}


def relaunch_command():
    """Program and arguments that start this dashboard again the way it was started.

    ``sys.orig_argv`` keeps ``-m face_attendance.dashboard`` (``sys.argv`` would
    point at ``__main__.py``, whose relative import fails when run as a file);
    in a frozen build ``sys.argv[0]`` is the executable and must not repeat.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, list(sys.argv[1:])
    original = getattr(sys, "orig_argv", None)
    if original:
        return sys.executable, list(original[1:])
    return sys.executable, list(sys.argv)


class _ShutdownWorker(QThread):
    """Runs the blocking stops; every stage is reported even if one of them fails."""

    stage = pyqtSignal(str)

    def __init__(self, engine, services):
        super().__init__()
        self._engine = engine
        self._services = tuple(services)

    def run(self):
        reported = []

        def report(name):
            reported.append(name)
            self.stage.emit(name)

        try:
            self._engine.shutdown(on_stage=report)
        except Exception:
            logging.exception("Engine shutdown failed")
        for name in ("capture", "saved"):
            if name not in reported:
                self.stage.emit(name)
        for service in self._services:
            try:
                service.stop()
            except Exception:
                logging.exception("Could not stop %r", service)
        self.stage.emit("services")


class _RingGlyph(QWidget):
    """Tinted disc with a glyph, orbited by a spinning arc that closes when done."""

    SIZE = 76

    def __init__(self, glyph, tone, theme, parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self._tone = tone
        self._theme = theme
        self.angle = 0.0
        self.complete = False
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        foreground, background = stat_tile_colors(self._theme, self._tone)
        size = float(self.SIZE)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        painter.drawEllipse(QRectF(10, 10, size - 20, size - 20))
        ring = QRectF(3, 3, size - 6, size - 6)
        track = QColor(foreground)
        track.setAlpha(45)
        painter.setPen(QPen(track, 3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(ring)
        pen = QPen(QColor(foreground), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        if self.complete:
            painter.drawEllipse(ring)
        else:
            painter.drawArc(ring, int(-self.angle * 16), int(110 * 16))
        glyph = make_pixmap(self._glyph, 28, foreground, 2.0, ratio=self.devicePixelRatioF())
        painter.drawPixmap(QRectF(size / 2 - 14, size / 2 - 14, 28, 28), glyph, QRectF(glyph.rect()))
        painter.end()


def _marker(kind, color, ratio, size=18):
    """Step marker: an open circle while pending, a filled check once done."""
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if kind == "done":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(1, 1, size - 2, size - 2))
        check = make_pixmap("check", 12, "#FFFFFF", 3.0, ratio=ratio)
        painter.drawPixmap(QRectF(3, 3, 12, 12), check, QRectF(check.rect()))
    else:
        painter.setPen(QPen(QColor(color), 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(3, 3, size - 6, size - 6))
    painter.end()
    return pixmap


class _StepRow(QWidget):
    def __init__(self, text, theme, parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        self._theme = theme
        self.state = "pending"
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.marker = QLabel()
        self.marker.setFixedSize(18, 18)
        row.addWidget(self.marker)
        self.label = QLabel(text)
        self.label.setObjectName("exitStep")
        self.label.setProperty("state", "pending")
        row.addWidget(self.label, 1)
        self.render_marker(0.0)

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        repolish(self.label, "state", state)
        self.render_marker(0.0)

    def render_marker(self, angle):
        c = palette(self._theme)
        ratio = self.devicePixelRatioF()
        if self.state == "active":
            pixmap = spinner_pixmap(angle, 18, c["primary"], ratio)
        elif self.state == "done":
            pixmap = _marker("done", c["success"], ratio)
        else:
            pixmap = _marker("pending", c["border"], ratio)
        self.marker.setPixmap(pixmap)


class ShutdownOverlay(WindowOverlay):
    """Progress card shown while the dashboard stops; it cannot be dismissed."""

    def __init__(self, mode="quit", theme="light", parent=None, detail=None):
        title, subtitle, glyph, tone, final_step, closing = MODES[mode]
        subtitle = detail or subtitle
        super().__init__(theme, parent, dismissible=False, title=title)
        self.mode = mode
        self._closing_title = closing
        column = self.column
        self.ring = _RingGlyph(glyph, tone, theme)
        column.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(14)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("dialogTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.title_label)
        column.addSpacing(6)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("dialogSubtitle")
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        column.addWidget(self.subtitle_label)
        column.addSpacing(20)
        self.progress = ProgressLine(theme, height=6, rounded=True)
        column.addWidget(self.progress)
        column.addSpacing(18)
        steps = QVBoxLayout()
        steps.setContentsMargins(4, 0, 4, 0)
        steps.setSpacing(10)
        self.steps = [_StepRow(text, theme) for text in (*STEP_LABELS, final_step)]
        for step in self.steps:
            steps.addWidget(step)
        column.addLayout(steps)
        column.addSpacing(18)
        self.note = QLabel("Attendance already captured is saved before the application closes.")
        self.note.setObjectName("exitNote")
        self.note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.note.setWordWrap(True)
        column.addWidget(self.note)

        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(900)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(self._on_spin)
        self._spin.start()

    def _on_spin(self, angle):
        angle = float(angle)
        self.ring.angle = angle
        self.ring.update()
        for step in self.steps:
            if step.state == "active":
                step.render_marker(angle)

    def activate(self, index):
        """Steps before *index* are done, *index* is running, the rest wait."""
        for position, step in enumerate(self.steps):
            step.set_state("done" if position < index else
                           "active" if position == index else "pending")

    def mark_slow(self):
        self.note.setText("Still finishing up — waiting for the camera and database to close…")

    def finish(self):
        self.activate(len(self.steps))
        self.ring.complete = True
        self.ring.update()
        self.title_label.setText(self._closing_title)
        self.subtitle_label.setText("Everything is stopped and all attendance is saved.")
        self.progress.finish("ok", 260)

    def done(self, result):
        self._spin.stop()
        super().done(result)


class ShutdownSequence(QObject):
    """Drives ``ShutdownOverlay`` from the worker's real stages, then says it is done.

    Each stage stays on screen for at least ``STEP_MIN_MS`` so an instant
    shutdown still reads as a smooth sequence rather than a flash.
    """

    finished = pyqtSignal(str)

    def __init__(self, window, mode, services=(), detail=None):
        super().__init__(window)
        self.mode = mode
        self._engine = window.engine
        self._services = tuple(services)
        self.overlay = ShutdownOverlay(mode, window.settings.theme, window, detail)
        self._queue = []
        self._worker = None
        self._gate = QTimer(self)
        self._gate.setSingleShot(True)
        self._gate.timeout.connect(self._pump)
        self._slow = QTimer(self)
        self._slow.setSingleShot(True)
        self._slow.setInterval(SLOW_AFTER_MS)
        self._slow.timeout.connect(self.overlay.mark_slow)

    def start(self):
        self.overlay.open()
        self.overlay.activate(0)
        self.overlay.progress.start(0.14, 320)
        self._gate.start(STEP_MIN_MS)
        self._slow.start()
        worker = _ShutdownWorker(self._engine, self._services)
        worker.stage.connect(self._post)
        self._worker = worker
        worker.start()

    def _post(self, stage):
        self._queue.append(stage)
        self._pump()

    def _pump(self):
        if self._gate.isActive() or not self._queue:
            return
        stage = self._queue.pop(0)
        self.overlay.activate(STAGES.index(stage) + 1)
        if stage == "services":
            self._slow.stop()
            self.overlay.progress.advance(0.9, FINAL_HOLD_MS)
            QTimer.singleShot(FINAL_HOLD_MS, self._finish)
        else:
            self.overlay.progress.advance(TARGETS[stage], 360)
            self._gate.start(STEP_MIN_MS)

    def _finish(self):
        self.overlay.finish()
        QTimer.singleShot(FINISH_BEAT_MS, self._complete)

    def _complete(self):
        settle(self._worker)
        self.finished.emit(self.mode)
