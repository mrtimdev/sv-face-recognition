"""Live Usage screen — real-time system resource monitoring.

Inspired by Docker Desktop's Resource Allocation view: clean cards,
progress bars, ring gauges, per-core heat grid, and live mini-graphs.
All stats collected on a background thread; UI updates on the main thread.
Supports both dark and light themes via the palette.
"""
import math
from collections import deque

from PyQt6.QtCore import QRectF, QPointF, Qt, QTimer
from PyQt6.QtGui import (QColor, QFont, QFontDatabase,
                          QLinearGradient, QPainter, QPainterPath, QPen)
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel,
                              QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..sysmon import Monitor, Snapshot, static_info
from ..theme import palette
from ..widgets import PageHeader

HISTORY_LEN = 60


def _fmt_bytes(b, decimals=1):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(b) < 1024:
            return f"{b:.{decimals}f} {unit}"
        b /= 1024
    return f"{b:.{decimals}f} PB"


def _fmt_speed(bps):
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 ** 2:.1f} MB/s"


def _fmt_uptime(sec):
    sec = int(sec)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, _ = divmod(sec, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _load_color(pct, theme="dark"):
    c = palette(theme)
    if pct < 60:
        return c["success"]
    if pct < 85:
        return c["warn"]
    return c["danger"]


def _get_font(size, bold=False, mono=False):
    available = set(QFontDatabase.families())
    if mono:
        family = next((n for n in ("SF Mono", "JetBrains Mono", "Cascadia Mono",
                                    "Consolas", "DejaVu Sans Mono", "Courier New")
                       if n in available), "monospace")
    else:
        family = next(
            (n for n in ("Helvetica Neue", "SF Pro Display", "Segoe UI",
                         "DejaVu Sans", "Arial")
             if n in available),
            QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).family())
    f = QFont(family, size)
    if bold:
        f.setWeight(QFont.Weight.Bold)
    return f


# ═══════════════════════════════════════════════════════════════════════════
#  Custom painted widgets
# ═══════════════════════════════════════════════════════════════════════════

class RingGauge(QWidget):
    """Animated ring gauge with percentage in the centre."""

    def __init__(self, label="", size=120, theme="dark", parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._label = label
        self._theme = theme
        self._value = 0.0
        self._display = 0.0

    def set_value(self, pct):
        self._value = max(0.0, min(100.0, pct))

    def set_theme(self, theme):
        self._theme = theme

    def paintEvent(self, _):
        self._display += (self._value - self._display) * 0.25
        c = palette(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = min(self.width(), self.height())
        cx, cy = self.width() / 2, self.height() / 2
        ring_w = 8
        r = (s - ring_w) / 2 - 4

        track = QColor(c["border"])
        p.setPen(QPen(track, ring_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)

        if self._display > 0.5:
            span = self._display / 100.0 * 360
            arc_color = QColor(_load_color(self._display, self._theme))
            p.setPen(QPen(arc_color, ring_w, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            rect = QRectF(cx - r, cy - r, r * 2, r * 2)
            p.drawArc(rect, int(90 * 16), int(-span * 16))

            glow = QColor(_load_color(self._display, self._theme))
            glow.setAlpha(25)
            p.setPen(QPen(glow, ring_w + 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(rect, int(90 * 16), int(-span * 16))

        p.setPen(QColor(c["text"]))
        p.setFont(_get_font(int(s * 0.18), bold=True))
        p.drawText(QRectF(0, 0, s, s * 0.55),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom,
                   f"{self._display:.0f}%")

        p.setPen(QColor(c["muted"]))
        p.setFont(_get_font(int(s * 0.09)))
        p.drawText(QRectF(0, s * 0.55, s, s * 0.2),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                   self._label)
        p.end()


class ResourceBar(QWidget):
    """Horizontal progress bar with label, value, and sub-text."""

    def __init__(self, label="", theme="dark", parent=None):
        super().__init__(parent)
        self._label = label
        self._theme = theme
        self._value = 0.0
        self._display = 0.0
        self._detail = ""
        self._sub = ""
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_value(self, pct, detail="", sub=""):
        self._value = max(0.0, min(100.0, pct))
        self._detail = detail
        self._sub = sub

    def set_theme(self, theme):
        self._theme = theme

    def paintEvent(self, _):
        self._display += (self._value - self._display) * 0.25
        c = palette(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r = 8, 8

        p.setPen(QColor(c["text_secondary"]))
        p.setFont(_get_font(11, bold=True))
        p.drawText(QRectF(pad_l, 0, w * 0.5, 20),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)
        p.setPen(QColor(c["text"]))
        p.setFont(_get_font(11, bold=True, mono=True))
        p.drawText(QRectF(w * 0.5, 0, w * 0.5 - pad_r, 20),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._detail)

        bar_y, bar_h = 24, 6
        bar_w = w - pad_l - pad_r
        track_rect = QRectF(pad_l, bar_y, bar_w, bar_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(c["border"]))
        p.drawRoundedRect(track_rect, 3, 3)

        if self._display > 0.5:
            fill_w = bar_w * (self._display / 100.0)
            fill_c = QColor(_load_color(self._display, self._theme))
            grad = QLinearGradient(pad_l, 0, pad_l + fill_w, 0)
            grad.setColorAt(0.0, fill_c.lighter(120))
            grad.setColorAt(1.0, fill_c)
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(pad_l, bar_y, fill_w, bar_h), 3, 3)

        if self._sub:
            p.setPen(QColor(c["muted"]))
            p.setFont(_get_font(9))
            p.drawText(QRectF(pad_l, bar_y + bar_h + 4, bar_w, 16),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._sub)

        p.end()


class CoreGrid(QWidget):
    """Per-core heat tiles — small squares that glow brighter with load."""

    def __init__(self, core_count=1, theme="dark", parent=None):
        super().__init__(parent)
        self._cores = max(1, core_count)
        self._theme = theme
        self._values = [0.0] * self._cores
        self._display = [0.0] * self._cores
        cols = min(self._cores, 8)
        rows = math.ceil(self._cores / cols)
        tile = 28
        gap = 4
        self.setFixedSize(cols * (tile + gap) - gap + 16, rows * (tile + gap) - gap + 16)

    def set_values(self, per_core):
        for i in range(min(len(per_core), self._cores)):
            self._values[i] = per_core[i]

    def set_theme(self, theme):
        self._theme = theme

    def paintEvent(self, _):
        c = palette(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tile, gap, pad = 28, 4, 8
        cols = min(self._cores, 8)
        is_light = self._theme == "light"

        for i in range(self._cores):
            self._display[i] += (self._values[i] - self._display[i]) * 0.25
            v = self._display[i]
            col = i % cols
            row = i // cols
            x = pad + col * (tile + gap)
            y = pad + row * (tile + gap)

            color = QColor(_load_color(v, self._theme))
            if is_light:
                alpha = int(30 + v * 1.8)
            else:
                alpha = int(40 + v * 2.1)
            color.setAlpha(min(255, alpha))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(x, y, tile, tile), 6, 6)

            if is_light:
                text_alpha = int(100 + v * 1.2)
                p.setPen(QColor(0, 0, 0, min(220, text_alpha)))
            else:
                text_alpha = int(80 + v * 1.5)
                p.setPen(QColor(255, 255, 255, min(255, text_alpha)))
            p.setFont(_get_font(8, bold=True))
            p.drawText(QRectF(x, y, tile, tile), Qt.AlignmentFlag.AlignCenter, str(i))
        p.end()


class MiniGraph(QWidget):
    """60-second sparkline graph."""

    def __init__(self, label="", color="#3B82F6", height=60, theme="dark", parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._theme = theme
        self._history = deque([0.0] * HISTORY_LEN, maxlen=HISTORY_LEN)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def push(self, value):
        self._history.append(value)

    def set_theme(self, theme):
        self._theme = theme

    def paintEvent(self, _):
        c = palette(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 8, 8, 18, 4
        gw = w - pad_l - pad_r
        gh = h - pad_t - pad_b

        p.setPen(QColor(c["muted"]))
        p.setFont(_get_font(9, bold=True))
        p.drawText(QRectF(pad_l, 0, gw * 0.5, pad_t),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        vals = list(self._history)
        cur = vals[-1] if vals else 0
        p.setPen(QColor(self._color))
        p.setFont(_get_font(9, bold=True, mono=True))
        p.drawText(QRectF(w * 0.5, 0, w * 0.5 - pad_r, pad_t),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   f"{cur:.1f}%" if cur < 1000 else _fmt_speed(cur))

        grid_pen = QPen(QColor(c["border"]), 1)
        p.setPen(grid_pen)
        for frac in (0.0, 0.5, 1.0):
            gy = pad_t + gh * (1 - frac)
            p.drawLine(QPointF(pad_l, gy), QPointF(w - pad_r, gy))

        if not vals or max(vals) == 0:
            p.end()
            return

        max_v = max(max(vals), 1)
        path = QPainterPath()
        fill_path = QPainterPath()
        first = True
        for i, v in enumerate(vals):
            x = pad_l + (i / max(len(vals) - 1, 1)) * gw
            y = pad_t + gh * (1 - v / max_v)
            pt = QPointF(x, y)
            if first:
                path.moveTo(pt)
                fill_path.moveTo(QPointF(x, pad_t + gh))
                fill_path.lineTo(pt)
                first = False
            else:
                path.lineTo(pt)
                fill_path.lineTo(pt)

        fill_path.lineTo(QPointF(pad_l + gw, pad_t + gh))
        fill_path.closeSubpath()
        fill_color = QColor(self._color)
        fill_color.setAlpha(20 if self._theme == "light" else 25)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill_color)
        p.drawPath(fill_path)

        line_color = QColor(self._color)
        p.setPen(QPen(line_color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

        p.end()


# ═══════════════════════════════════════════════════════════════════════════
#  Usage screen
# ═══════════════════════════════════════════════════════════════════════════

def _make_card(title=""):
    card = QFrame()
    card.setObjectName("card")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("cardTitle")
        layout.addWidget(lbl)
        divider = QFrame()
        divider.setObjectName("cardDivider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)
    return card, layout


class UsageScreen(QWidget):
    """Live system usage screen with ring gauges, bars, core grid and graphs."""

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._theme = getattr(settings, "theme", "dark")
        self._monitor = None
        self._static = None
        self._snap = Snapshot()

        self._build()

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(64)
        self._refresh_timer.timeout.connect(self._repaint_animated)

    # ── construction ──────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(16)

        self.header = PageHeader("", "Real-time system resource monitoring.")
        outer.addWidget(self.header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setObjectName("scrollFeed")
        body = QWidget()
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        t = self._theme

        # ── ring gauges row ───────────────────────────────────────────────
        gauges_card, gauges_layout = _make_card("Resource Overview")
        gauge_row = QHBoxLayout()
        gauge_row.setSpacing(24)
        gauge_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cpu_ring = RingGauge("CPU", 120, theme=t)
        self._ram_ring = RingGauge("RAM", 120, theme=t)
        self._gpu_ring = RingGauge("GPU", 120, theme=t)
        gauge_row.addWidget(self._cpu_ring)
        gauge_row.addWidget(self._ram_ring)
        gauge_row.addWidget(self._gpu_ring)
        gauges_layout.addLayout(gauge_row)
        self._body.addWidget(gauges_card)

        # ── resource bars ─────────────────────────────────────────────────
        bars_card, bars_layout = _make_card("Resource Allocation")
        self._cpu_bar = ResourceBar("CPU Usage", theme=t)
        self._ram_bar = ResourceBar("Memory", theme=t)
        self._swap_bar = ResourceBar("Swap", theme=t)
        self._disk_bar = ResourceBar("Disk Usage", theme=t)
        for bar in (self._cpu_bar, self._ram_bar, self._swap_bar, self._disk_bar):
            bars_layout.addWidget(bar)
        self._body.addWidget(bars_card)

        # ── per-core grid ─────────────────────────────────────────────────
        core_card, core_layout = _make_card("CPU Cores")
        self._core_grid = CoreGrid(1, theme=t)
        self._core_label = QLabel("")
        self._core_label.setObjectName("emptyBody")
        core_layout.addWidget(self._core_grid, 0, Qt.AlignmentFlag.AlignCenter)
        core_layout.addWidget(self._core_label, 0, Qt.AlignmentFlag.AlignCenter)
        self._body.addWidget(core_card)

        # ── live graphs ───────────────────────────────────────────────────
        graph_card, graph_layout = _make_card("Live Graphs (60s)")
        self._cpu_graph = MiniGraph("CPU %", "#3B82F6", 70, theme=t)
        self._ram_graph = MiniGraph("RAM %", "#22C55E", 70, theme=t)
        self._net_down_graph = MiniGraph("Network Down", "#A78BFA", 70, theme=t)
        self._net_up_graph = MiniGraph("Network Up", "#FBBF24", 70, theme=t)
        for g in (self._cpu_graph, self._ram_graph, self._net_down_graph, self._net_up_graph):
            graph_layout.addWidget(g)
        self._body.addWidget(graph_card)

        # ── info strip ────────────────────────────────────────────────────
        info_card, info_layout = _make_card("System Info")
        self._info_grid = QGridLayout()
        self._info_grid.setSpacing(8)
        self._info_labels = {}
        info_items = [
            ("cpu_name", "CPU"),
            ("gpu_name", "GPU"),
            ("os", "Platform"),
            ("uptime", "Uptime"),
            ("battery", "Battery"),
            ("app_ram", "App Memory"),
            ("disk_io", "Disk I/O"),
            ("net_speed", "Network"),
        ]
        for i, (key, title) in enumerate(info_items):
            row, col = divmod(i, 2)
            title_lbl = QLabel(title)
            title_lbl.setObjectName("statTitle")
            title_lbl.setFixedWidth(80)
            value_lbl = QLabel("—")
            value_lbl.setObjectName("statValue")
            value_lbl.setStyleSheet("font-size: 13px;")
            cell = QHBoxLayout()
            cell.setSpacing(8)
            cell.addWidget(title_lbl)
            cell.addWidget(value_lbl, 1)
            self._info_grid.addLayout(cell, row, col)
            self._info_labels[key] = value_lbl
        info_layout.addLayout(self._info_grid)
        self._body.addWidget(info_card)

        self._body.addStretch(1)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        self._start_monitor()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._stop_monitor()

    def close(self):
        self._stop_monitor()
        super().close()

    def _start_monitor(self):
        if self._monitor is not None:
            return
        if self._static is None:
            self._static = static_info()
            cores = self._static.cpu_cores_logical
            self._core_grid.deleteLater()
            core_card = self._body.itemAt(2).widget()
            core_layout = core_card.layout()
            self._core_grid = CoreGrid(cores, theme=self._theme)
            core_layout.insertWidget(1, self._core_grid, 0, Qt.AlignmentFlag.AlignCenter)
            self._core_label.setText(
                f"{cores} logical cores  •  {self._static.cpu_cores_physical} physical")
            if not self._static.gpu_name:
                self._gpu_ring.hide()
            self._update_static_info()

        self._monitor = Monitor(interval=1.0)
        self._monitor.snapshotReady.connect(self._on_snapshot)
        self._monitor.start()
        self._refresh_timer.start()

    def _stop_monitor(self):
        self._refresh_timer.stop()
        if self._monitor:
            self._monitor.stop()
            self._monitor = None

    def _on_snapshot(self, snap):
        self._snap = snap

        self._cpu_ring.set_value(snap.cpu_percent)
        self._ram_ring.set_value(snap.ram_percent)
        if snap.gpu_available:
            self._gpu_ring.set_value(snap.gpu_percent)
            self._gpu_ring.show()

        freq = f" @ {snap.cpu_freq_mhz:.0f} MHz" if snap.cpu_freq_mhz else ""
        self._cpu_bar.set_value(snap.cpu_percent, f"{snap.cpu_percent:.1f}%{freq}",
                                f"{len(snap.cpu_per_core)} threads active")
        self._ram_bar.set_value(
            snap.ram_percent,
            f"{_fmt_bytes(snap.ram_used_bytes)} / {_fmt_bytes(snap.ram_total_bytes)}",
            f"{snap.ram_percent:.1f}% used")
        self._swap_bar.set_value(
            snap.swap_percent,
            f"{_fmt_bytes(snap.swap_used_bytes)} / {_fmt_bytes(snap.swap_total_bytes)}",
            f"{snap.swap_percent:.1f}% used" if snap.swap_total_bytes else "No swap configured")
        self._disk_bar.set_value(
            snap.disk_percent,
            f"{_fmt_bytes(snap.disk_used_bytes)} / {_fmt_bytes(snap.disk_total_bytes)}",
            f"{snap.disk_percent:.1f}% used")

        if snap.cpu_per_core:
            self._core_grid.set_values(snap.cpu_per_core)

        self._cpu_graph.push(snap.cpu_percent)
        self._ram_graph.push(snap.ram_percent)
        self._net_down_graph.push(snap.net_recv_bytes_sec)
        self._net_up_graph.push(snap.net_sent_bytes_sec)

        self._info_labels["uptime"].setText(_fmt_uptime(snap.uptime_sec))
        if snap.battery_percent is not None:
            state = "Charging" if snap.battery_charging else "Battery"
            self._info_labels["battery"].setText(f"{snap.battery_percent:.0f}% ({state})")
        else:
            self._info_labels["battery"].setText("No battery")
        self._info_labels["app_ram"].setText(_fmt_bytes(snap.app_ram_bytes))
        self._info_labels["disk_io"].setText(
            f"R: {_fmt_speed(snap.disk_read_bytes_sec)}  W: {_fmt_speed(snap.disk_write_bytes_sec)}")
        self._info_labels["net_speed"].setText(
            f"↓ {_fmt_speed(snap.net_recv_bytes_sec)}  ↑ {_fmt_speed(snap.net_sent_bytes_sec)}")

    def _update_static_info(self):
        if not self._static:
            return
        s = self._static
        self._info_labels["cpu_name"].setText(s.cpu_name or "—")
        self._info_labels["gpu_name"].setText(s.gpu_name or "No GPU detected")
        self._info_labels["os"].setText(f"{s.os_name} ({s.hostname})")

    def _repaint_animated(self):
        self._cpu_ring.update()
        self._ram_ring.update()
        self._gpu_ring.update()
        self._cpu_bar.update()
        self._ram_bar.update()
        self._swap_bar.update()
        self._disk_bar.update()
        self._core_grid.update()
        self._cpu_graph.update()
        self._ram_graph.update()
        self._net_down_graph.update()
        self._net_up_graph.update()

    # ── theme ─────────────────────────────────────────────────────────────

    def set_theme(self, theme):
        self._theme = theme
        for w in (self._cpu_ring, self._ram_ring, self._gpu_ring,
                  self._cpu_bar, self._ram_bar, self._swap_bar, self._disk_bar,
                  self._core_grid,
                  self._cpu_graph, self._ram_graph, self._net_down_graph, self._net_up_graph):
            w.set_theme(theme)

    def on_settings_changed(self, settings):
        self.settings = settings
        self.set_theme(settings.theme)
