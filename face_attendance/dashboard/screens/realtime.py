"""Real-time Attendance: live check-in feed, per-face states and alerts."""
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                             QListWidget, QListWidgetItem, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from ...report import AttendanceReader, preset_range
from ..sound import SoundPlayer
from ..theme import palette
from ..widgets import StatCard, ToastBar, VideoView


TRACK_HEADERS = ("Track", "Employee", "Employee ID", "State", "Progress", "Cooldown")
RECORD_HEADERS = ("Recorded", "Employee", "Employee ID", "Presence", "Evidence image")
STATE_TONES = {"SUCCESS": "ok", "CONFIRMED": "ok", "VERIFYING": "warn", "CAPTURING": "warn",
               "COOLDOWN": "warn", "UNKNOWN": "bad", "ERROR": "bad", "DETECTING": "idle",
               "RECOGNIZING": "idle", "READY": "idle"}


class RealtimeScreen(QWidget):
    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.reader = AttendanceReader(self.settings.db_path)
        self.tracks = []
        self.unknown_seen = set()
        self.records = []
        self.cards = {}
        self._prev_track_count = 0
        self._sound = SoundPlayer(settings.alert_path, cooldown_sec=1.5)
        self._build()
        self._connect()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(self.reload)
        self.refresh_timer.start()
        self.track_timer = QTimer(self)
        self.track_timer.setInterval(500)
        self.track_timer.timeout.connect(self._render_tracks)
        self.track_timer.start()
        self.reload()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("Real-time Attendance")
        title.setObjectName("screenTitle")
        self.subtitle = QLabel("Live check-ins, per-face verification state and unknown-face alerts")
        self.subtitle.setObjectName("screenSubtitle")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        header.addWidget(QLabel("Feed range"))
        self.range_box = QComboBox()
        self.range_box.addItem("Last hour", "hour")
        self.range_box.addItem("Today", "today")
        self.range_box.addItem("Last 7 days", "week")
        self.range_box.setCurrentIndex(1)
        self.refresh_button = QPushButton("Refresh now")
        header.addWidget(self.range_box)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setSpacing(10)
        for index, (key, title, value) in enumerate((("checked", "Check-ins in range", "0"),
                                                    ("employees", "Employees present", "0"),
                                                    ("faces", "Faces in view", "0"),
                                                    ("unknown", "Unknown-face alerts", "0"),
                                                    ("pending", "Outbox pending", "0"))):
            card = StatCard(title, value)
            self.cards[key] = card
            grid.addWidget(card, 0, index)
        layout.addLayout(grid)

        upper = QHBoxLayout()
        upper.setSpacing(12)
        self.video = VideoView(self, message="No camera feed",
                               subtitle="Start the engine on the Live Monitor")
        upper.addWidget(self.video, 3)
        tracks_column = QVBoxLayout()
        tracks_title = QLabel("Faces being verified")
        tracks_title.setObjectName("sectionTitle")
        self.tracks_table = QTableWidget(0, len(TRACK_HEADERS))
        self.tracks_table.setHorizontalHeaderLabels(TRACK_HEADERS)
        self.tracks_table.verticalHeader().setVisible(False)
        self.tracks_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tracks_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tracks_column.addWidget(tracks_title)
        tracks_column.addWidget(self.tracks_table, 1)
        upper.addLayout(tracks_column, 1)
        layout.addLayout(upper, 3)

        lower = QHBoxLayout()
        lower.setSpacing(12)
        records_column = QVBoxLayout()
        records_title = QLabel("Committed attendance")
        records_title.setObjectName("sectionTitle")
        self.records_table = QTableWidget(0, len(RECORD_HEADERS))
        self.records_table.setHorizontalHeaderLabels(RECORD_HEADERS)
        self.records_table.verticalHeader().setVisible(False)
        self.records_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.records_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.records_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        records_column.addWidget(records_title)
        records_column.addWidget(self.records_table, 1)
        lower.addLayout(records_column, 3)
        alerts_column = QVBoxLayout()
        alerts_title = QLabel("Unknown faces (this session)")
        alerts_title.setObjectName("sectionTitle")
        self.alerts = QListWidget()
        self.alerts.setAlternatingRowColors(True)
        alerts_column.addWidget(alerts_title)
        alerts_column.addWidget(self.alerts, 1)
        lower.addLayout(alerts_column, 1)
        layout.addLayout(lower, 2)

        self.toast = ToastBar()
        layout.addWidget(self.toast)

    def _connect(self):
        self.engine.frameReady.connect(self.video.set_frame)
        self.engine.statsReady.connect(self.on_stats)
        self.engine.tracksReady.connect(self.on_tracks)
        self.engine.captureTriggered.connect(self.on_capture)
        self.engine.attendanceSaved.connect(self.on_saved)
        self.engine.errorRaised.connect(lambda message: self.toast.show_message(message, "bad"))
        self.refresh_button.clicked.connect(self.reload)
        self.range_box.currentIndexChanged.connect(lambda _: self.reload())
        self.records_table.cellDoubleClicked.connect(self._open_snapshot)

    # --- engine signals ---------------------------------------------------
    def on_stats(self, stats):
        self.cards["faces"].set_value(stats.get("faces", 0))
        max_faces = stats.get("max_detect_faces", 5)
        self.cards["faces"].set_hint(
            f"{stats.get('known_faces', 0)} verified  |  max {max_faces}")
        if not stats.get("storage_ready", True):
            self.toast.show_message("Attendance storage is not ready.", "bad")

    def on_tracks(self, tracks):
        self.tracks = tracks
        self._note_unknown(tracks)

    def on_capture(self, job):
        """Play a capture sound and show feedback when a face is auto-grabbed."""
        self.toast.show_message(
            f"Captured {job.employee_name} - saving...", "warn")
        self._sound.play()

    def on_saved(self, result):
        self.toast.show_message(f"{result.job.employee_name} checked in "
                                f"({result.job.duration:.1f}s verified)", "ok")
        self.reload()

    def _note_unknown(self, tracks):
        """Session-only alert list; unknown faces are never persisted."""
        for snapshot in tracks:
            if snapshot["name"] != "UNKNOWN" or not snapshot["visible"]:
                continue
            if snapshot["track_id"] in self.unknown_seen:
                continue
            self.unknown_seen.add(snapshot["track_id"])
            stamp = datetime.now().strftime("%H:%M:%S")
            self.alerts.insertItem(0, QListWidgetItem(
                f"{stamp}  face #{snapshot['track_id']} not enrolled ({snapshot['age']:.0f}s in view)"))
            while self.alerts.count() > 100:
                self.alerts.takeItem(self.alerts.count() - 1)
            self.cards["unknown"].set_value(len(self.unknown_seen))

    def _render_tracks(self):
        """Update the tracks table, reusing existing items where possible to
        avoid widget churn and reduce layout recalculation overhead."""
        rows = self.tracks
        count = len(rows)
        # Only resize the table when the row count changes.
        if count != self._prev_track_count:
            self.tracks_table.setRowCount(count)
            self._prev_track_count = count
        colors = palette(self.settings.theme)
        for index, snapshot in enumerate(rows):
            state = snapshot["state"]
            values = (f"#{snapshot['track_id']}", snapshot["name"], snapshot["employee_id"] or "-",
                      state, f"{snapshot['progress'] * 100:.0f}%",
                      f"{snapshot['cooldown']:.0f}s" if snapshot["cooldown"] else "-")
            for column, value in enumerate(values):
                text = str(value)
                existing = self.tracks_table.item(index, column)
                if existing is not None:
                    # Reuse the existing item — just update its text and colour.
                    if existing.text() != text:
                        existing.setText(text)
                    if column == 3:
                        tone = STATE_TONES.get(state, "idle")
                        color = QColor(colors["accent" if tone == "ok" else
                                                "warn" if tone == "warn" else
                                                "danger" if tone == "bad" else "muted"])
                        if existing.foreground().color() != color:
                            existing.setForeground(color)
                else:
                    item = QTableWidgetItem(text)
                    if column == 3:
                        tone = STATE_TONES.get(state, "idle")
                        item.setForeground(QColor(colors["accent" if tone == "ok" else
                                                          "warn" if tone == "warn" else
                                                          "danger" if tone == "bad" else "muted"]))
                    self.tracks_table.setItem(index, column, item)

    # --- committed attendance ---------------------------------------------
    def reload(self):
        preset = self.range_box.currentData() or "today"
        start, end = preset_range(preset)
        self.records = self.reader.records(start=start, end=end, limit=200)
        summary = self.reader.summary(start=start, end=end)
        counts = self.reader.outbox_counts()
        self.cards["checked"].set_value(summary["records"])
        self.cards["employees"].set_value(summary["employees"])
        self.cards["pending"].set_value(counts["pending"], "warn" if counts["pending"] else "ok")
        self.cards["pending"].set_hint(f"{counts['synced']} synced to the backend")
        if self.reader.error:
            self.toast.show_message(self.reader.error, "bad")
        self._render_records()

    def _render_records(self):
        self.records_table.setRowCount(len(self.records))
        for index, record in enumerate(self.records):
            values = (record["time"], record["name"], record["employee_id"],
                      f"{record['duration']:.1f}s",
                      Path(record["snapshot"]).name if record["snapshot"] else "-")
            for column, value in enumerate(values):
                self.records_table.setItem(index, column, QTableWidgetItem(str(value)))

    def _open_snapshot(self, row, column):
        if not 0 <= row < len(self.records):
            return
        path = Path(self.records[row]["snapshot"])
        if not path.exists():
            self.toast.show_message("That evidence image is not available on this machine.", "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def on_settings_changed(self, settings):
        """Reopen the database when the dashboard points at another file."""
        self.settings = settings
        self.reader.close()
        self.reader = AttendanceReader(self.settings.db_path)
        self._sound = SoundPlayer(settings.alert_path, cooldown_sec=1.5)
        self.reload()

    def closeEvent(self, event):
        self.reader.close()
        self.refresh_timer.stop()
        self.track_timer.stop()
        self._sound.stop()
        super().closeEvent(event)

    def set_theme(self, theme):
        self.video.set_theme(theme)
        for card in self.cards.values():
            card.set_theme(theme)
