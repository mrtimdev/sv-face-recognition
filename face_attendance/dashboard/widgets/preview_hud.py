"""Camera controls shared by the embedded and fullscreen live preview."""
from time import monotonic

import numpy as np
from PyQt6.QtCore import QPoint, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QWidget

from ..icons import make_icon, make_pixmap


def _outlined_text(painter, rect, text, font, foreground, halo, alignment=Qt.AlignmentFlag.AlignCenter):
    """A fine opposite-color keyline protects lettering on mixed footage."""
    painter.setFont(font)
    metrics = painter.fontMetrics()
    text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, max(0, int(rect.width())))
    x = rect.left()
    if alignment == Qt.AlignmentFlag.AlignCenter:
        x += (rect.width() - metrics.horizontalAdvance(text)) / 2
    baseline = rect.center().y() + (metrics.ascent() - metrics.descent()) / 2
    path = QPainterPath()
    path.addText(QPointF(x, baseline), font, text)
    pen = QPen(QColor(halo), 2.5, Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    painter.strokePath(path, pen)
    painter.fillPath(path, QColor(foreground))


class _ContrastLabel(QLabel):
    def __init__(self, text=""):
        super().__init__(text)
        self._light_ink = True

    def set_contrast(self, light_ink):
        if self._light_ink != light_ink:
            self._light_ink = light_ink
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.DemiBold)
        _outlined_text(painter, QRectF(self.rect()), self.text(), font,
                       "#FFFFFF" if self._light_ink else "#101827",
                       "#101827" if self._light_ink else "#FFFFFF",
                       Qt.AlignmentFlag.AlignLeft)
        painter.end()


class _CameraButton(QToolButton):
    """A transparent circular control; all visual marks are strokes or glyphs."""
    def __init__(self, glyph, parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self._light_ink = True
        self._primary = False
        self._recording = False
        self._pixmaps = {}
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.set_glyph(glyph)

    def set_glyph(self, glyph):
        self._glyph = glyph
        self._pixmaps.clear()
        self.setIcon(make_icon(glyph, 24, "#FFFFFF"))
        self.update()

    def set_contrast(self, light_ink):
        if self._light_ink != light_ink:
            self._light_ink = light_ink
            self._pixmaps.clear()
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        light = self._light_ink
        foreground, halo = ("#FFFFFF", "#101827") if light else ("#101827", "#FFFFFF")
        accent = "#93C5FD" if light else "#1D4ED8"
        if not self.isEnabled():
            painter.setOpacity(.55)
        labeled = bool(self.text())
        diameter = min(self.width() - 10, 64 if self._primary else 42) if labeled else 34
        y = 70 - diameter if labeled else (self.height() - diameter) / 2
        circle = QRectF((self.width() - diameter) / 2, y, diameter, diameter)
        if self.isDown():
            circle = circle.adjusted(2, 2, -2, -2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        # Opposing keylines remain legible over both highlights and shadows.
        painter.setPen(QPen(QColor(halo), 3.5 if self._primary else 2.5))
        painter.drawEllipse(circle)
        painter.setPen(QPen(QColor(accent if self._primary else foreground),
                            2 if self._primary or self.underMouse() else 1))
        painter.drawEllipse(circle)
        if self._primary or self.hasFocus() or self.underMouse():
            orbit = circle.adjusted(-4, -4, 4, 4)
            pen = QPen(QColor(accent if self._primary else foreground), 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(orbit, 28 * 16, 64 * 16)
            painter.drawArc(orbit, 208 * 16, 64 * 16)
        size = 30 if self._primary and diameter > 50 else 22
        glyph_color = ("#FB7185" if light else "#BE123C") if self._recording else foreground
        for color, stroke in ((halo, 4.2), (glyph_color, 2.0)):
            key = (self._glyph, size, color, stroke, self.devicePixelRatioF())
            if key not in self._pixmaps:
                self._pixmaps[key] = make_pixmap(self._glyph, size, color, stroke, self.devicePixelRatioF())
            painter.drawPixmap(QPointF(circle.center().x() - size / 2,
                                       circle.center().y() - size / 2), self._pixmaps[key])
        if labeled:
            font = QFont(self.font())
            font.setPixelSize(11)
            font.setWeight(QFont.Weight.DemiBold)
            if self.text() == "Auto Record" and self.width() < 78:
                for text, top in (("Auto", 75), ("Record", 88)):
                    _outlined_text(painter, QRectF(0, top, self.width(), 13), text, font, foreground, halo)
            else:
                _outlined_text(painter, QRectF(0, 80, self.width(), 20), self.text(), font, foreground, halo)
        painter.end()


class PreviewHUD(QWidget):
    def __init__(self, video):
        super().__init__(video)
        self.setObjectName("previewHUD")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._sample_at = 0.0
        self._luma = None
        self._frame_size = None
        self._brightness = {}
        self._source = ""
        self._status = "STOPPED"
        self._state = None
        self.top = QFrame(self)
        self.top.setObjectName("previewTop")
        row = QHBoxLayout(self.top)
        row.setContentsMargins(12, 8, 8, 8)
        row.setSpacing(8)
        self.status = _ContrastLabel("●  STOPPED")
        row.addWidget(self.status)
        self.source = _ContrastLabel()
        self.source.setMinimumWidth(0)
        row.addWidget(self.source, 1)
        self.metrics = _ContrastLabel()
        row.addWidget(self.metrics)
        self.flash = self._button("bolt", "Preview capture flash")
        self.snapshot = self._button("camera", "Save snapshot")
        self.settings = self._button("gear", "Camera settings")
        self.captures = self._button("folder", "Open captures folder")
        self.fullscreen = self._button("expand", "Enter full screen")
        for button in (self.flash, self.snapshot, self.captures, self.settings, self.fullscreen):
            row.addWidget(button)

        self.dock = QFrame(self)
        self.dock.setObjectName("previewDock")
        dock_row = QHBoxLayout(self.dock)
        dock_row.setContentsMargins(4, 4, 4, 4)
        dock_row.setSpacing(4)
        self.record = self._button("dot", "Start engine", "Start")
        self.capture = self._button("camera", "Save snapshot", "Snapshot")
        self.pause = self._button("pause", "Pause attendance recording; preview stays live", "Pause")
        self.pause.setObjectName("previewPrimary")
        self.pause._primary = True
        self.restart = self._button("restart", "Restart engine", "Restart")
        self.more = self._button("more", "More monitor actions", "More")
        for button in (self.record, self.capture, self.pause, self.restart, self.more):
            dock_row.addWidget(button, 1)
        self._style()

    def _button(self, icon, tooltip, label=""):
        button = _CameraButton(icon, self)
        button.setIconSize(QSize(24, 24))
        button.setText(label)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        if label:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setMinimumWidth(44)
            button.setFixedHeight(104)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        else:
            button.setFixedSize(40, 40)
        return button

    def _style(self):
        self.setStyleSheet("""
            QWidget#previewHUD, QFrame#previewTop, QFrame#previewDock,
            QLabel, QToolButton { background: transparent; border: none; }
            QLabel { font-size: 11px; font-weight: 600; }
            QToolButton::menu-indicator { image: none; width: 0; }
        """)

    def reset_contrast(self):
        self._luma = None
        self._frame_size = None
        self._sample_at = 0.0
        self._brightness.clear()
        for widget in self.findChildren((_CameraButton, _ContrastLabel)):
            widget.set_contrast(True)

    def sample_frame(self, frame):
        """Measure linear luminance under each control without changing the image.

        A small cached sample accounts for aspect-fit letterboxing. Hysteresis
        and a four-Hz limit keep the foreground from flickering near mid-gray.
        """
        now = monotonic()
        if frame is None or not getattr(frame, "size", 0) or now < self._sample_at:
            return
        self._sample_at = now + .25
        self._frame_size = QSize(frame.shape[1], frame.shape[0])
        sample = frame[::max(1, frame.shape[0] // 64), ::max(1, frame.shape[1] // 64), :3] / 255.0
        linear = np.where(sample <= .04045, sample / 12.92, ((sample + .055) / 1.055) ** 2.4)
        self._luma = linear @ np.array([.0722, .7152, .2126])
        self._update_contrast()

    def _update_contrast(self):
        if self._luma is None:
            return
        target = self.parentWidget()._target(self._frame_size)
        if target.isEmpty():
            return
        for widget in self.findChildren((_CameraButton, _ContrastLabel)):
            if widget.isHidden():
                continue
            origin = widget.mapTo(self, QPoint(0, 0))
            xs = np.linspace(origin.x(), origin.x() + widget.width() - 1, 12)
            ys = np.linspace(origin.y(), origin.y() + widget.height() - 1, 12)
            xs, ys = np.meshgrid((xs - target.x()) / target.width(),
                                 (ys - target.y()) / target.height())
            inside = (xs >= 0) & (xs < 1) & (ys >= 0) & (ys < 1)
            values = np.zeros(xs.shape)
            values[inside] = self._luma[(ys[inside] * self._luma.shape[0]).astype(int),
                                        (xs[inside] * self._luma.shape[1]).astype(int)]
            luminance = float(values.mean())
            previous = self._brightness.get(widget, luminance)
            luminance = previous * .25 + luminance * .75
            self._brightness[widget] = luminance
            if luminance > .23:
                widget.set_contrast(False)
            elif luminance < .16:
                widget.set_contrast(True)

    def set_source(self, source):
        self._source = source
        self.source.setToolTip(source)
        self._elide_source()

    def set_fullscreen(self, fullscreen):
        self.fullscreen.set_glyph("x" if fullscreen else "expand")
        label = "Exit full screen" if fullscreen else "Enter full screen"
        self.fullscreen.setToolTip(label)
        self.fullscreen.setAccessibleName(label)

    def _elide_source(self):
        self.source.setText(self.source.fontMetrics().elidedText(
            self._source, Qt.TextElideMode.ElideRight, max(0, self.source.width())))

    def set_metrics(self, resolution, fps):
        if self._status == "STOPPED":
            self.metrics.setText("No camera feed")
            return
        self.metrics.setText(f"{resolution}  ·  {fps:.1f} FPS" if resolution else f"{fps:.1f} FPS")

    def set_state(self, running, paused, status, storage_ready=False, has_frame=False):
        state = (running, paused, status, storage_ready, has_frame)
        if state == self._state:
            return
        self._state = state
        label = ("PAUSED" if paused else "LIVE") if status == "CONNECTED" and running else status
        if not running:
            label = "STOPPED"
        self._status = label
        self.status.setText(f"●  {'OFFLINE' if label == 'CAMERA DISCONNECTED' else label}")
        self.status.setToolTip(label)
        self.record.setText("Auto Record" if running and not paused and storage_ready and status == "CONNECTED"
                            else "Standby" if running else "Start")
        self.record._recording = self.record.text() == "Auto Record"
        self.record.set_glyph("dot" if running else "play")
        self.record.setToolTip("Stop engine" if running else "Start engine")
        self.record.setAccessibleName(self.record.toolTip())
        self.pause.setEnabled(running)
        self.pause.setText("Resume" if paused else "Pause")
        self.pause.set_glyph("play" if paused else "pause")
        self.pause.setToolTip("Resume attendance recording" if paused else
                              "Pause attendance recording; preview stays live")
        self.pause.setAccessibleName(self.pause.toolTip())
        for button in (self.snapshot, self.capture):
            button.setEnabled(running and has_frame and status == "CONNECTED")
        if not running:
            self.metrics.setText("No camera feed")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width, height = self.width(), self.height()
        margin = 12
        self.metrics.setVisible(width >= 760)
        self.flash.setVisible(width >= 530)
        self.snapshot.setVisible(width >= 650)
        self.captures.setVisible(width >= 380)
        self.top.setGeometry(margin, margin, max(0, width - margin * 2), 54)
        dock_width = min(600, width - margin * 2)
        self.dock.setGeometry((width - dock_width) // 2, height - 124, dock_width, 112)
        self.top.layout().activate()
        self.dock.layout().activate()
        self._elide_source()
        self._update_contrast()
