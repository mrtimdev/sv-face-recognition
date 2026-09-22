"""Recent-activity feed: avatar, name, meta line, timestamp and tone badge."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..theme import palette
from .avatar import Avatar


class ActivityRow(QFrame):
    def __init__(self, theme="light", parent=None):
        super().__init__(parent)
        self.setObjectName("activityRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        self.avatar = Avatar("?", size=34, theme=theme)
        layout.addWidget(self.avatar)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self.name_label = QLabel("")
        self.name_label.setObjectName("activityName")
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("activityDetail")
        text_col.addWidget(self.name_label)
        text_col.addWidget(self.detail_label)
        layout.addLayout(text_col, 1)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(4)
        meta_col.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.time_label = QLabel("")
        self.time_label.setObjectName("activityTime")
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.badge = QLabel("")
        self.badge.setObjectName("chip")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setVisible(False)
        meta_col.addWidget(self.time_label)
        meta_col.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(meta_col)
        self.set_theme(theme)

    def update_row(self, name, detail="", status="", tone="ok", time_text="", subtitle=""):
        self.avatar.set_name(name)
        self.name_label.setText(str(name))
        extra = " • ".join(part for part in (subtitle, detail) if part)
        self.detail_label.setText(extra)
        self.detail_label.setVisible(bool(extra))
        self.time_label.setText(str(time_text))
        self.badge.setText(str(status))
        self.badge.setProperty("tone", tone if status else "idle")
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
        self.badge.setVisible(bool(status))

    def set_theme(self, theme):
        self.avatar.set_theme(theme)
        colors = palette(theme)
        for widget, key in ((self.name_label, "text"), (self.detail_label, "muted"),
                            (self.time_label, "muted")):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.setStyleSheet(f"color: {colors[key]};")


class ActivityFeed(QWidget):
    """Newest-first list of activity rows with a bounded length."""

    def __init__(self, theme="light", max_rows=20, empty_text="No activity yet",
                 parent=None):
        super().__init__(parent)
        self._theme = theme
        self._max_rows = max_rows
        self._rows = []
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(2)
        self.empty_label = QLabel(empty_text)
        self.empty_label.setObjectName("emptyBody")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout_.addWidget(self.empty_label)

    def add(self, name, detail="", status="", tone="ok", time_text="", subtitle=""):
        row = ActivityRow(self._theme)
        row.update_row(name, detail, status, tone, time_text, subtitle)
        self.layout_.insertWidget(0, row)
        self._rows.insert(0, row)
        self.empty_label.setVisible(False)
        while len(self._rows) > self._max_rows:
            old = self._rows.pop()
            self.layout_.removeWidget(old)
            old.deleteLater()

    def clear(self):
        for row in self._rows:
            self.layout_.removeWidget(row)
            row.deleteLater()
        self._rows = []
        self.empty_label.setVisible(True)

    def count(self):
        return len(self._rows)

    def set_empty_text(self, text):
        self.empty_label.setText(text)

    def set_theme(self, theme):
        self._theme = theme
        for row in self._rows:
            row.set_theme(theme)
