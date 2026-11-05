"""Compact, newest-first attendance activity with a bounded row count."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..theme import palette
from .activity_detail import EvidencePhoto
from .stat_card import IconTile


class ActivityRow(QFrame):
    activated = pyqtSignal(object)

    def __init__(self, theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("activityRow")
        self.setFixedHeight(100)
        self._tone = "ok"
        self._texts = {}
        self.entry = {}
        self._pressed = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 12, 10, 12)
        layout.setSpacing(10)

        self.accent = QFrame()
        self.accent.setObjectName("activityAccent")
        self.accent.setFixedWidth(3)
        layout.addWidget(self.accent)
        self.avatar = EvidencePhoto(theme=theme, thumbnail=True)
        self.avatar.setFixedSize(48, 48)
        self.avatar.setToolTip("Attendance capture · Open details")
        layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)

        text_col = QVBoxLayout()
        text_col.setSpacing(5)
        title = QHBoxLayout()
        title.setSpacing(6)
        self.name_label = self._label("activityName")
        title.addWidget(self.name_label, 1)
        self.time_label = QLabel("")
        self.time_label.setObjectName("activityTime")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        title.addWidget(self.time_label)
        text_col.addLayout(title)
        self.detail_label = self._label("activityDetail")
        text_col.addWidget(self.detail_label)
        self.badge = QLabel("")
        self.badge.setObjectName("activityBadge")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text_col.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addLayout(text_col, 1)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.set_theme(theme)

    def _label(self, object_name):
        label = QLabel("")
        label.setObjectName(object_name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        return label

    def update_row(self, name, detail="", status="", tone="ok", time_text="", subtitle="", evidence=None):
        self.entry = dict(evidence or {})
        self.entry.update(name=str(name), employee_id=str(subtitle), detail=str(detail),
                          status=str(status), time=str(time_text))
        capture = self.entry.get("capture")
        if capture is not None:
            self.avatar.set_photo(capture)
        self.setAccessibleName(f"Attendance details for {name}, {status}, {time_text}")
        self._texts = {
            self.name_label: str(name),
            self.detail_label: " · ".join(str(part) for part in (subtitle, detail) if part),
        }
        for label, text in self._texts.items():
            label.setToolTip(text)
        self.time_label.setText(str(time_text))
        self.badge.setText(str(status))
        self.badge.setVisible(bool(status))
        self._tone = tone
        self.set_theme(self._theme)
        self._elide()

    def mousePressEvent(self, event):
        self._pressed = event.button() == Qt.MouseButton.LeftButton
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pressed and event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.activated.emit(self.entry)
        self._pressed = False
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit(self.entry)
            event.accept()
            return
        super().keyPressEvent(event)

    def _elide(self):
        for label, text in self._texts.items():
            label.setText(label.fontMetrics().elidedText(
                text, Qt.TextElideMode.ElideRight, max(0, label.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.layout().activate()
        self._elide()

    def set_theme(self, theme):
        self._theme = theme
        self.avatar.set_theme(theme)
        c = palette(theme)
        tone = {"ok": "success", "bad": "danger", "warn": "warn"}.get(self._tone, "info")
        badge_color = ({"success": "#15803D", "danger": "#B91C1C", "warn": "#92400E", "info": "#1D4ED8"}
                       [tone] if theme == "light" else c[tone])
        self.setStyleSheet(f"""
            QFrame#activityRow {{ background: {c['card_alt']};
                border: 1px solid {c['border_soft']}; border-radius: 12px; }}
            QFrame#activityRow:hover {{ background: {c['hover']}; border-color: {c['border']}; }}
            QFrame#activityRow:focus {{ border: 1px solid {c['primary']}; }}
            QFrame#activityAccent {{ background: {c[tone]}; border: none; border-radius: 1px; }}
            QLabel {{ background: transparent; border: none; }}
            QLabel#activityName {{ color: {c['text']}; font-size: 13px; font-weight: 600; }}
            QLabel#activityDetail, QLabel#activityTime {{ color: {c['text_secondary']}; font-size: 11px; }}
            QLabel#activityBadge {{ background: {c[tone + '_bg']}; color: {badge_color};
                border-radius: 8px; padding: 3px 8px; font-size: 10px; font-weight: 600; }}
        """)
        self._elide()


class ActivityFeed(QWidget):
    """Rows keep their natural height; remaining space belongs to the empty area."""

    rowActivated = pyqtSignal(object)

    def __init__(self, theme="light", max_rows=20, empty_text="Ready for check-ins", parent=None):
        super().__init__(parent)
        self.setObjectName("activityFeed")
        self._theme = theme
        self._max_rows = max_rows
        self._rows = []
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(8)
        self.empty_state = QWidget()
        self.empty_state.setObjectName("activityEmpty")
        empty = QVBoxLayout(self.empty_state)
        empty.setContentsMargins(16, 24, 16, 24)
        empty.setSpacing(12)
        empty.addStretch(1)
        self.empty_icon = IconTile("check-circle", tone="blue", size=52, theme=theme)
        empty.addWidget(self.empty_icon, 0, Qt.AlignmentFlag.AlignCenter)
        self.empty_label = QLabel(empty_text)
        self.empty_label.setObjectName("activityEmptyTitle")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(self.empty_label)
        self.empty_detail = QLabel("Saved check-ins and recording issues will appear here as they happen.")
        self.empty_detail.setObjectName("activityEmptyDetail")
        self.empty_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_detail.setWordWrap(True)
        empty.addWidget(self.empty_detail)
        empty.addStretch(1)
        self.layout_.addWidget(self.empty_state, 1)
        self.layout_.addStretch(0)
        self.set_theme(theme)

    def add(self, name, detail="", status="", tone="ok", time_text="", subtitle="", evidence=None):
        row = ActivityRow(self._theme)
        row.update_row(name, detail, status, tone, time_text, subtitle, evidence)
        row.activated.connect(self.rowActivated.emit)
        self.layout_.insertWidget(0, row)
        self._rows.insert(0, row)
        self.empty_state.hide()
        self.layout_.setStretch(self.layout_.count() - 1, 1)
        while len(self._rows) > self._max_rows:
            old = self._rows.pop()
            self.layout_.removeWidget(old)
            old.deleteLater()

    def clear(self):
        for row in self._rows:
            self.layout_.removeWidget(row)
            row.deleteLater()
        self._rows = []
        self.layout_.setStretch(self.layout_.count() - 1, 0)
        self.empty_state.show()

    def count(self):
        return len(self._rows)

    def set_empty_text(self, text):
        self.empty_label.setText(text)

    def set_theme(self, theme):
        self._theme = theme
        c = palette(theme)
        self.setStyleSheet(f"""
            QWidget#activityFeed, QWidget#activityEmpty {{ background: transparent; }}
            QLabel#activityEmptyTitle {{ color: {c['text']}; font-size: 14px; font-weight: 600; }}
            QLabel#activityEmptyDetail {{ color: {c['text_secondary']}; font-size: 12px; }}
        """)
        self.empty_icon.set_tone("blue", theme)
        for row in self._rows:
            row.set_theme(theme)
