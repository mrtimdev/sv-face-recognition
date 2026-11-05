"""Header notification centre: bell button, session feed and dropdown panel.

``NotificationCenter`` keeps the newest events of this session in memory.  The
badge counts *unseen* items and clears when the panel opens; rows stay
highlighted as *unread* until the panel closes or they are opened, so a quick
glance never loses track of what arrived.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import count

from PyQt6.QtCore import (QEasingCurve, QObject, QRectF, QSize, Qt, QTimer,
                          QVariantAnimation, pyqtSignal)
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..icons import make_icon, make_pixmap
from ..theme import palette, stat_tile_colors
from .form import repolish
from .popup import PopupPanel
from .stat_card import IconTile

# kind -> (glyph, tone, filter category, tile tone)
KINDS = {
    "checkin": ("check", "ok", "checkins", "green"),
    "failed": ("x", "bad", "alerts", "red"),
    "unknown": ("face-id", "warn", "alerts", "orange"),
    "error": ("alert", "bad", "alerts", "red"),
    "update": ("download", "info", "alerts", "blue"),
    "update_failed": ("alert", "warn", "alerts", "orange"),
}
FILTERS = (("all", "All"), ("checkins", "Check-ins"), ("alerts", "Alerts"))
TONE_KEYS = {"ok": "success", "bad": "danger", "warn": "warn", "info": "info"}


def relative_time(moment, now=None):
    """Short, human timestamps: "Just now", "5 min ago", "14:02", "Yesterday 09:10"."""
    now = now or datetime.now()
    seconds = (now - moment).total_seconds()
    if seconds < 45:
        return "Just now"
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min ago"
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    if moment.date() == (now - timedelta(days=1)).date():
        return "Yesterday " + moment.strftime("%H:%M")
    return moment.strftime("%d %b")


@dataclass
class Notification:
    uid: int
    kind: str
    title: str
    body: str
    created: datetime
    thumbnail: object = None
    payload: dict = field(default_factory=dict)
    seen: bool = False
    read: bool = False
    repeat: int = 1

    @property
    def glyph(self):
        return KINDS.get(self.kind, KINDS["error"])[0]

    @property
    def tone(self):
        return KINDS.get(self.kind, KINDS["error"])[1]

    @property
    def category(self):
        return KINDS.get(self.kind, KINDS["error"])[2]

    @property
    def tile_tone(self):
        return KINDS.get(self.kind, KINDS["error"])[3]


class NotificationCenter(QObject):
    """Bounded, newest-first feed; repeated identical alerts collapse into one row."""

    changed = pyqtSignal()
    added = pyqtSignal(object)

    LIMIT = 50
    COALESCE_SEC = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self._ids = count(1)

    def add(self, kind, title, body="", thumbnail=None, payload=None, when=None):
        when = when or datetime.now()
        latest = self._items[0] if self._items else None
        if (latest is not None and kind != "checkin" and latest.kind == kind
                and latest.title == title and latest.body == body
                and (when - latest.created).total_seconds() < self.COALESCE_SEC):
            latest.repeat += 1
            latest.created = when
            latest.seen = latest.read = False
            item = latest
        else:
            item = Notification(next(self._ids), kind, str(title), str(body), when,
                                thumbnail, dict(payload or {}))
            self._items.insert(0, item)
            del self._items[self.LIMIT:]
        self.changed.emit()
        self.added.emit(item)
        return item

    def items(self, category="all"):
        return [item for item in self._items if category == "all" or item.category == category]

    def get(self, uid):
        return next((item for item in self._items if item.uid == uid), None)

    def unseen_count(self):
        return sum(not item.seen for item in self._items)

    def unread_count(self):
        return sum(not item.read for item in self._items)

    def mark_seen(self):
        if any(not item.seen for item in self._items):
            for item in self._items:
                item.seen = True
            self.changed.emit()

    def mark_read(self, uid=None):
        changed = False
        for item in self._items:
            if (uid is None or item.uid == uid) and not (item.read and item.seen):
                item.read = item.seen = True
                changed = True
        if changed:
            self.changed.emit()

    def clear(self):
        if self._items:
            self._items.clear()
            self.changed.emit()

    def __len__(self):
        return len(self._items)


class _BellGlyph(QWidget):
    """Bell that swings from its crown when rung."""

    def __init__(self, button):
        super().__init__(button)
        self._button = button
        self.angle = 0.0
        self.setFixedSize(24, 24)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        c = palette(self._button._theme)
        color = c["primary"] if self._button.property("open") else c["text_secondary"]
        ratio = self.devicePixelRatioF()
        pixmap = make_pixmap("bell", 20, color, 1.8, ratio=ratio)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.translate(12, 4)
        painter.rotate(self.angle)
        painter.translate(-12, -4)
        painter.drawPixmap(2, 2, pixmap)
        painter.end()


class NotificationButton(QPushButton):
    """Header bell with an unread bubble and a short ring on new events."""

    def __init__(self, theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("headerIconButton")
        self.setFixedSize(44, 44)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Notifications")
        self._theme = theme
        self._count = 0
        self.glyph = _BellGlyph(self)
        self.badge = QLabel("", self)
        self.badge.setObjectName("notifBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.badge.hide()
        self._ring = QVariantAnimation(self)
        self._ring.setDuration(900)
        self._ring.setStartValue(0.0)
        self._ring.setEndValue(0.0)
        for step, angle in ((0.1, 16), (0.24, -14), (0.38, 11), (0.52, -8), (0.66, 5), (0.8, -2)):
            self._ring.setKeyValueAt(step, float(angle))
        self._ring.setEasingCurve(QEasingCurve.Type.Linear)
        self._ring.valueChanged.connect(self._swing)
        self.set_theme(theme)

    def count(self):
        return self._count

    def set_count(self, value, ring=True):
        value = max(0, int(value or 0))
        grew = value > self._count
        self._count = value
        self.badge.setText(str(value) if value < 100 else "99+")
        self.badge.setVisible(value > 0)
        self.setAccessibleDescription(f"{value} unread" if value else "No new notifications")
        self.setToolTip(f"Notifications • {value} new" if value else "Notifications")
        self._place()
        if grew and ring:
            self.ring()

    def ring(self):
        self._ring.stop()
        self._ring.start()

    def set_open(self, is_open):
        repolish(self, "open", bool(is_open))
        self.glyph.update()

    def set_theme(self, theme):
        self._theme = theme
        c = palette(theme)
        self.badge.setStyleSheet(
            f"QLabel#notifBadge {{ background-color: {c['badge_bg']}; color: {c['badge_fg']};"
            f" border: 2px solid {c['panel_alt']}; border-radius: 10px;"
            " font-size: 10px; font-weight: 700; padding: 0px 4px; }")
        self.glyph.update()

    def _swing(self, angle):
        self.glyph.angle = float(angle)
        self.glyph.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place()

    def _place(self):
        self.glyph.move((self.width() - self.glyph.width()) // 2,
                        (self.height() - self.glyph.height()) // 2)
        width = max(20, self.badge.fontMetrics().horizontalAdvance(self.badge.text()) + 12)
        self.badge.setFixedSize(width, 20)
        self.badge.move(self.width() - width - 1, 1)


class _NotificationGlyph(QWidget):
    """Face thumbnail with a tone badge, or a tinted icon tile when there is no photo."""

    SIZE = 44

    def __init__(self, item, theme, parent=None):
        super().__init__(parent)
        self._item = item
        self._theme = theme
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def paintEvent(self, event):
        c = palette(self._theme)
        item = self._item
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        photo = item.thumbnail
        box = QRectF(0, 0, 40, 40)
        if photo is not None and not photo.isNull():
            path = QPainterPath()
            path.addRoundedRect(box, 11, 11)
            painter.save()
            painter.setClipPath(path)
            scaled = photo.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                  Qt.TransformationMode.SmoothTransformation)
            source = QRectF((scaled.width() - 80) / 2, (scaled.height() - 80) / 2, 80, 80)
            painter.drawPixmap(box, scaled, source)
            painter.restore()
            badge = QRectF(26, 26, 18, 18)
            painter.setPen(QPen(QColor(c["card"]), 2))
            painter.setBrush(QColor(c[TONE_KEYS.get(item.tone, "info")]))
            painter.drawEllipse(badge.adjusted(1, 1, -1, -1))
            glyph = make_pixmap(item.glyph, 10, "#FFFFFF", 3.0, ratio=self.devicePixelRatioF())
            painter.drawPixmap(QRectF(30, 30, 10, 10), glyph, QRectF(glyph.rect()))
        else:
            foreground, background = stat_tile_colors(self._theme, item.tile_tone)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(background))
            painter.drawRoundedRect(box, 12, 12)
            glyph = make_pixmap(item.glyph, 20, foreground, 1.9, ratio=self.devicePixelRatioF())
            painter.drawPixmap(QRectF(10, 10, 20, 20), glyph, QRectF(glyph.rect()))
        painter.end()


class _UnreadDot(QWidget):
    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setFixedSize(8, 8)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette(self._theme)["primary"]))
        painter.drawEllipse(QRectF(0, 0, 8, 8))
        painter.end()


class NotificationRow(QFrame):
    activated = pyqtSignal(object)

    def __init__(self, item, theme="light", parent=None):
        super().__init__(parent)
        self.item = item
        self._theme = theme
        self._pressed = False
        self.setObjectName("notifRow")
        self.setProperty("unread", not item.read)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{item.title}. {item.body}")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 10, 12, 10)
        row.setSpacing(12)
        self.glyph = _NotificationGlyph(item, theme)
        row.addWidget(self.glyph, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(3)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        self.title_label = self._label("notifTitle")
        title_row.addWidget(self.title_label, 1)
        self.time_label = QLabel("")
        self.time_label.setObjectName("notifTime")
        title_row.addWidget(self.time_label, 0, Qt.AlignmentFlag.AlignTop)
        text.addLayout(title_row)
        body_row = QHBoxLayout()
        body_row.setSpacing(8)
        self.body_label = self._label("notifBody")
        body_row.addWidget(self.body_label, 1)
        self.dot = _UnreadDot(theme, self)
        if item.read:
            self.dot.hide()
        body_row.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        text.addLayout(body_row)
        row.addLayout(text, 1)

        title = item.title + (f"  ×{item.repeat}" if item.repeat > 1 else "")
        self._texts = {self.title_label: title, self.body_label: item.body}
        for label, value in self._texts.items():
            label.setToolTip(value)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.refresh_time()

    @staticmethod
    def _label(object_name):
        label = QLabel("")
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def refresh_time(self, now=None):
        self.time_label.setText(relative_time(self.item.created, now))

    def _elide(self):
        for label, value in self._texts.items():
            label.setText(label.fontMetrics().elidedText(
                value, Qt.TextElideMode.ElideRight, max(0, label.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.layout().activate()
        self._elide()

    def mousePressEvent(self, event):
        self._pressed = event.button() == Qt.MouseButton.LeftButton
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._pressed and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.activated.emit(self.item)
        self._pressed = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.item)
            event.accept()
            return
        super().keyPressEvent(event)


class NotificationPanel(PopupPanel):
    """Dropdown listing the session's check-ins and alerts."""

    activated = pyqtSignal(object)
    reportRequested = pyqtSignal()

    LIST_MAX_HEIGHT = 404

    def __init__(self, center, theme="light", parent=None):
        super().__init__(width=392, theme=theme, parent=parent)
        self.center = center
        self._filter = "all"
        self._rows = []

        header = QHBoxLayout()
        header.setContentsMargins(18, 16, 12, 8)
        header.setSpacing(8)
        title = QLabel("Notifications")
        title.setObjectName("popupTitle")
        header.addWidget(title)
        self.count_pill = QLabel("")
        self.count_pill.setObjectName("countPill")
        self.count_pill.setFixedHeight(20)
        header.addWidget(self.count_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addStretch(1)
        self.mark_button = QPushButton("  Mark all read")
        self.mark_button.setObjectName("ghostButton")
        self.mark_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mark_button.clicked.connect(self._mark_all_read)
        header.addWidget(self.mark_button)
        self.body.addLayout(header)

        segments = QFrame()
        segments.setObjectName("segmentBar")
        segment_row = QHBoxLayout(segments)
        segment_row.setContentsMargins(3, 3, 3, 3)
        segment_row.setSpacing(2)
        self.segment_group = QButtonGroup(self)
        self.segment_group.setExclusive(True)
        self.segments = {}
        for index, (key, label) in enumerate(FILTERS):
            button = QPushButton(label)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setChecked(key == self._filter)
            self.segment_group.addButton(button, index)
            self.segments[key] = button
            segment_row.addWidget(button, 1)
        self.segment_group.idClicked.connect(self._on_segment)
        wrap = QHBoxLayout()
        wrap.setContentsMargins(16, 4, 16, 10)
        wrap.addWidget(segments)
        self.body.addLayout(wrap)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("plain")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_host = QWidget()
        self.list_host.setObjectName("plain")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(8, 0, 8, 6)
        self.list_layout.setSpacing(2)
        self.scroll.setWidget(self.list_host)
        self.body.addWidget(self.scroll)

        self.empty = QWidget()
        self.empty.setObjectName("plain")
        empty_layout = QVBoxLayout(self.empty)
        empty_layout.setContentsMargins(24, 26, 24, 30)
        empty_layout.setSpacing(8)
        self.empty_tile = IconTile("inbox", tone="blue", size=52, theme=theme)
        empty_layout.addWidget(self.empty_tile, 0, Qt.AlignmentFlag.AlignHCenter)
        empty_layout.addSpacing(4)
        self.empty_title = QLabel("You're all caught up")
        self.empty_title.setObjectName("emptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        self.empty_body = QLabel("")
        self.empty_body.setObjectName("emptyBody")
        self.empty_body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_body.setWordWrap(True)
        empty_layout.addWidget(self.empty_body)
        self.body.addWidget(self.empty)

        divider = QFrame()
        divider.setObjectName("hLine")
        divider.setFixedHeight(1)
        self.body.addWidget(divider)
        footer = QHBoxLayout()
        footer.setContentsMargins(10, 8, 12, 10)
        self.clear_button = QPushButton("Clear all")
        self.clear_button.setObjectName("ghostButton")
        self.clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_button.clicked.connect(self.center.clear)
        footer.addWidget(self.clear_button)
        footer.addStretch(1)
        self.report_button = QPushButton("View attendance report  ")
        self.report_button.setObjectName("linkButton")
        self.report_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.report_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.report_button.clicked.connect(self._request_report)
        footer.addWidget(self.report_button)
        self.body.addLayout(footer)

        self._clock = QTimer(self)
        self._clock.setInterval(30_000)
        self._clock.timeout.connect(self._refresh_times)
        self.center.changed.connect(self._on_changed)
        self.set_theme(theme)

    # --- api -----------------------------------------------------------------
    def _on_segment(self, index):
        self.set_filter(FILTERS[index][0])

    def _mark_all_read(self):
        self.center.mark_read()

    def set_filter(self, key):
        self._filter = key
        self.segments[key].setChecked(True)
        self.rebuild()

    def rows(self):
        return list(self._rows)

    def set_report_link_visible(self, visible):
        self.report_button.setVisible(bool(visible))

    def rebuild(self):
        while self.list_layout.count():
            entry = self.list_layout.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = []
        items = self.center.items(self._filter)
        fresh = [item for item in items if not item.read]
        earlier = [item for item in items if item.read]
        for heading, group in (("NEW", fresh), ("EARLIER", earlier)):
            if not group:
                continue
            label = QLabel(heading)
            label.setObjectName("notifSection")
            label.setContentsMargins(10, 8, 0, 4)
            self.list_layout.addWidget(label)
            label.show()
            for item in group:
                row = NotificationRow(item, self._theme)
                row.activated.connect(self._activate)
                self.list_layout.addWidget(row)
                # Children added to a visible parent are only shown on the next
                # event-loop turn; show now so the height below counts them.
                row.show()
                self._rows.append(row)
        self.list_layout.addStretch(1)

        has_items = bool(items)
        self.scroll.setVisible(has_items)
        self.empty.setVisible(not has_items)
        self.empty_body.setText({
            "all": "New check-ins and alerts will show up here as they happen.",
            "checkins": "Verified check-ins from this session will appear here.",
            "alerts": "Unknown faces and recording problems will appear here.",
        }[self._filter])
        unread = self.center.unread_count()
        self.count_pill.setText(f"{unread} new")
        self.count_pill.setVisible(unread > 0)
        self.mark_button.setEnabled(unread > 0)
        self.clear_button.setEnabled(len(self.center) > 0)
        if has_items:
            self.list_host.adjustSize()
            content = self.list_layout.sizeHint().height()
            self.scroll.setFixedHeight(min(self.LIST_MAX_HEIGHT, content))
        self.card.adjustSize()
        self.adjustSize()

    def set_theme(self, theme):
        super().set_theme(theme)
        c = palette(theme)
        if hasattr(self, "mark_button"):
            self.mark_button.setIcon(make_icon("check-all", 15, c["text_secondary"], ratio=2.0))
            self.mark_button.setIconSize(QSize(15, 15))
            self.report_button.setIcon(make_icon("arrow-right", 14, c["primary"], 2.0, ratio=2.0))
            self.report_button.setIconSize(QSize(14, 14))
            self.empty_tile.set_tone("blue", theme)
            if self.isVisible():
                self.rebuild()

    # --- events ----------------------------------------------------------------
    def showEvent(self, event):
        self.rebuild()
        self._clock.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._clock.stop()
        super().hideEvent(event)

    def _on_changed(self):
        if self.isVisible():
            self.rebuild()

    def _refresh_times(self):
        now = datetime.now()
        for row in self._rows:
            row.refresh_time(now)

    def _activate(self, item):
        self.center.mark_read(item.uid)
        self.close()
        self.activated.emit(item)

    def _request_report(self):
        self.close()
        self.reportRequested.emit()
