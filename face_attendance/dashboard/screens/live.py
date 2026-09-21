"""Live Monitor: preview, engine control and live statistics."""
from datetime import datetime
from pathlib import Path

import cv2
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QFileDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QPushButton, QVBoxLayout, QWidget)

from ...settings import describe_source
from ..widgets import StatCard, StatusPill, ToastBar, VideoView


CARD_DEFINITIONS = (
    ("camera", "Camera", "-", "Source"),
    ("camera_fps", "Camera FPS", "-", "Reported by the driver"),
    ("preview_fps", "Preview FPS", "-", "Frames rendered"),
    ("faces", "Faces in view", "-", "Known and unknown"),
    ("known", "Verified", "-", "Identity confirmed"),
    ("presence", "Presence", "0.0s", "Verified time in view"),
    ("enrolled", "Enrolled", "-", "Employees in the catalog"),
    ("latency", "Recognition", "-", "Last detection and encoding"),
    ("queue", "Storage queue", "-", "Evidence jobs waiting"),
    ("cooldowns", "Cooldowns", "-", "Employees waiting"),
)

STATUS_TONES = {"CONNECTED": "ok", "CONNECTING": "warn", "RECONNECTING": "warn",
                "CAMERA DISCONNECTED": "bad", "STOPPED": "idle"}


class LiveScreen(QWidget):
    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.cards = {}
        self._build()
        self._connect()
        self._sync_running(engine.running)

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("Live Monitor")
        title.setObjectName("screenTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("screenSubtitle")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        self.camera_pill = StatusPill("CONNECTING", "warn")
        self.engine_pill = StatusPill("STOPPED", "idle")
        header.addWidget(self.camera_pill)
        header.addWidget(self.engine_pill)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setSpacing(10)
        for index, (key, title, value, hint) in enumerate(CARD_DEFINITIONS):
            card = StatCard(title, value, hint)
            self.cards[key] = card
            grid.addWidget(card, index // 5, index % 5)
        layout.addLayout(grid)

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.video = VideoView()
        left.addWidget(self.video, 1)
        self.toast = ToastBar()
        left.addWidget(self.toast)

        controls = QHBoxLayout()
        self.start_button = QPushButton("Start engine")
        self.start_button.setObjectName("primary")
        self.pause_button = QPushButton("Pause recording")
        self.restart_button = QPushButton("Restart engine")
        self.snapshot_button = QPushButton("Save snapshot")
        self.captures_button = QPushButton("Open captures folder")
        for button in (self.start_button, self.pause_button, self.restart_button,
                       self.snapshot_button, self.captures_button):
            controls.addWidget(button)
        controls.addStretch(1)
        left.addLayout(controls)
        self.problem = QLabel("")
        self.problem.setObjectName("screenSubtitle")
        self.problem.setWordWrap(True)
        left.addWidget(self.problem)
        body.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(6)
        activity_title = QLabel("Recent activity")
        activity_title.setObjectName("sectionTitle")
        self.activity = QListWidget()
        self.activity.setAlternatingRowColors(True)
        right.addWidget(activity_title)
        right.addWidget(self.activity, 1)
        body.addLayout(right, 1)
        layout.addLayout(body, 1)
        self._update_subtitle()

    def _connect(self):
        self.engine.frameReady.connect(self.on_frame)
        self.engine.statsReady.connect(self.on_stats)
        self.engine.attendanceSaved.connect(self.on_saved)
        self.engine.attendanceFailed.connect(self.on_failed)
        self.engine.runningChanged.connect(self._sync_running)
        self.engine.errorRaised.connect(self.on_error)
        self.engine.logMessage.connect(self.on_log)
        self.start_button.clicked.connect(self._toggle_engine)
        self.pause_button.clicked.connect(self._toggle_pause)
        self.restart_button.clicked.connect(self._restart)
        self.snapshot_button.clicked.connect(self._save_snapshot)
        self.captures_button.clicked.connect(self._open_captures)

    # --- engine control ---------------------------------------------------
    def _toggle_engine(self):
        try:
            if self.engine.running:
                self.engine.stop()
            else:
                self.engine.start()
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            self.on_error(str(exc))

    def _toggle_pause(self):
        self.engine.toggle_paused()
        self._update_pause_button()

    def _restart(self):
        try:
            self.engine.restart()
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            self.on_error(str(exc))
        self._update_pause_button()

    def _save_snapshot(self):
        frame = self.engine.grab_clean_frame()
        if frame is None:
            self.toast.show_message("No clean camera frame yet; start the engine first.", "warn")
            return
        default = str(Path(self.settings.capture_dir) /
                      f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg")
        target, _ = QFileDialog.getSaveFileName(self, "Save snapshot", default, "JPEG image (*.jpg)")
        if not target:
            return
        ok = cv2.imwrite(target, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        self.toast.show_message(f"Snapshot saved to {target}" if ok else "Could not write the snapshot",
                                "ok" if ok else "bad")

    def _open_captures(self):
        directory = Path(self.settings.capture_dir)
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    # --- slots ------------------------------------------------------------
    def on_frame(self, frame):
        self.video.set_frame(frame)

    def on_stats(self, stats):
        self.cards["camera"].set_value(str(stats.get("status", "-")),
                                       STATUS_TONES.get(stats.get("status", ""), "idle"))
        self.cards["camera"].set_hint(stats.get("source", ""))
        self.cards["camera_fps"].set_value(f"{stats.get('camera_fps', 0):.1f}")
        self.cards["preview_fps"].set_value(f"{stats.get('preview_fps', 0):.1f}")
        faces = stats.get("faces", 0)
        self.cards["faces"].set_value(faces)
        self.cards["faces"].set_hint(f"{stats.get('unknown_faces', 0)} unknown")
        self.cards["known"].set_value(stats.get("known_faces", 0),
                                      "ok" if stats.get("known_faces") else "idle")
        presence = stats.get("presence", 0.0)
        target = max(0.1, float(stats.get("capture_after", 3.0)))
        self.cards["presence"].set_value(f"{presence:.1f}s", "ok" if presence >= target else "idle")
        self.cards["presence"].set_hint(f"Needs {target:.1f}s verified")
        self.cards["enrolled"].set_value(stats.get("enrolled", 0))
        self.cards["enrolled"].set_hint(describe_source(self.settings.source))
        self.cards["latency"].set_value(f"{stats.get('latency_ms', 0):.0f}ms")
        queue, queue_max = stats.get("queue", 0), max(1, stats.get("queue_max", 1))
        self.cards["queue"].set_value(f"{queue}/{queue_max}",
                                      "bad" if queue >= queue_max else "ok" if queue == 0 else "warn")
        self.cards["cooldowns"].set_value(stats.get("cooldowns", 0))

        status = stats.get("status", "STOPPED")
        self.camera_pill.set_status(status, STATUS_TONES.get(status, "idle"))
        problems = [text for text in (stats.get("storage_error", ""),
                                      stats.get("recognition_error", "")) if text]
        if not stats.get("storage_ready", False):
            problems.insert(0, "Attendance storage is not ready; recording is disabled.")
        self.problem.setText(" | ".join(problems))

    def on_saved(self, result):
        recorded = datetime.fromtimestamp(result.recorded_at or 0).strftime("%H:%M:%S")
        self._log(f"{recorded}  {result.job.employee_name} ({result.job.employee_id}) recorded, "
                  f"{result.job.duration:.1f}s verified")
        self.toast.show_message(f"Attendance recorded for {result.job.employee_name}", "ok")

    def on_failed(self, result):
        self._log(f"Attendance save failed: {result.error}")
        self.toast.show_message(f"Attendance save failed: {result.error}", "bad")

    def on_error(self, message):
        self._log(str(message))
        self.toast.show_message(str(message), "bad")

    def on_log(self, message):
        self._log(str(message))

    def _sync_running(self, running):
        self.engine_pill.set_status("RUNNING" if running else "STOPPED", "ok" if running else "idle")
        self.start_button.setText("Stop engine" if running else "Start engine")
        self.pause_button.setEnabled(bool(running))
        self._update_pause_button()
        if not running:
            self.video.show_message("Engine stopped",
                                    "Press Start engine to open the camera and record attendance")
            self.camera_pill.set_status("STOPPED", "idle")
            for key in ("camera_fps", "preview_fps", "faces", "known", "cooldowns"):
                self.cards[key].set_value("-")
        else:
            self.video.show_message("Waiting for the camera",
                                    "The preview appears as soon as frames arrive")

    def _update_pause_button(self):
        paused = self.engine.paused
        self.pause_button.setText("Resume recording" if paused else "Pause recording")
        self.pause_button.setObjectName("primary" if paused else "")
        self.pause_button.style().unpolish(self.pause_button)
        self.pause_button.style().polish(self.pause_button)
        if self.engine.running:
            self.engine_pill.set_status("PAUSED" if paused else "RUNNING",
                                        "warn" if paused else "ok")

    def _update_subtitle(self):
        self.subtitle.setText(f"Source: {describe_source(self.settings.source)}  |  "
                              f"Database: {self.settings.db_path}")

    def _log(self, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.activity.insertItem(0, QListWidgetItem(f"{stamp}  {message}"))
        while self.activity.count() > 200:
            self.activity.takeItem(self.activity.count() - 1)
        self.activity.scrollToTop()

    def set_theme(self, theme):
        self.video.set_theme(theme)
        for card in self.cards.values():
            card.set_theme(theme)