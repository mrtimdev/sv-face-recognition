"""Live Monitor: preview, engine control, live statistics, system status and logs."""
from datetime import datetime
from pathlib import Path

import cv2
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QPushButton,
                             QVBoxLayout, QWidget)

from ...settings import describe_source
from ..sound import SoundPlayer
from ..widgets import StatCard, StatusPill, ToastBar, VideoView


STATUS_TONES = {"CONNECTED": "ok", "CONNECTING": "warn", "RECONNECTING": "warn",
                "CAMERA DISCONNECTED": "bad", "STOPPED": "idle"}


class LiveScreen(QWidget):
    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.cards = {}
        self._sound = SoundPlayer(settings.alert_path, cooldown_sec=1.5)
        self._build()
        self._connect()
        self._sync_running(engine.running)

    # ── build ────────────────────────────────────────────────────────

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(14)

        # Title row
        title_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title = QLabel("Live Monitor")
        title.setObjectName("screenTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("screenSubtitle")
        title_col.addWidget(title)
        title_col.addWidget(self.subtitle)
        title_row.addLayout(title_col)
        title_row.addStretch(1)
        self.camera_pill = StatusPill("CONNECTING", "warn")
        self.engine_pill = StatusPill("STOPPED", "idle")
        title_row.addWidget(self.camera_pill)
        title_row.addWidget(self.engine_pill)
        root.addLayout(title_row)

        # ── Stat cards (4 with icons) ───────────────────────────────
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        card_defs = [
            ("enrolled", "Enrolled Faces", "-", "\U0001F464", "#2563EB"),
            ("verified", "Verified Today", "0", "✅", "#16A34A"),
            ("waiting", "Waiting", "0", "⏳", "#D97706"),
            ("unknown", "Unknown Faces", "0", "\U0001F6A8", "#DC2626"),
        ]
        for key, label, val, icon, color in card_defs:
            card = StatCard(label, val, icon=icon, icon_color=color)
            self.cards[key] = card
            cards_row.addWidget(card)
        root.addLayout(cards_row)

        # ── Main body: camera + right panel ─────────────────────────
        body = QHBoxLayout()
        body.setSpacing(14)

        # Camera Preview Panel
        cam_panel = QFrame()
        cam_panel.setObjectName("panel")
        cam_lay = QVBoxLayout(cam_panel)
        cam_lay.setContentsMargins(0, 0, 0, 0)
        cam_lay.setSpacing(0)

        cam_header = QHBoxLayout()
        cam_header.setContentsMargins(14, 10, 14, 10)
        cam_title = QLabel("Camera Preview")
        cam_title.setObjectName("sectionTitle")
        cam_header.addWidget(cam_title)
        self.live_badge = QLabel("LIVE")
        self.live_badge.setObjectName("liveBadge")
        self.live_badge.setFixedHeight(22)
        cam_header.addWidget(self.live_badge)
        cam_header.addStretch(1)
        self.face_count_label = QLabel("0 faces")
        self.face_count_label.setObjectName("faceBadge")
        cam_header.addWidget(self.face_count_label)
        cam_lay.addLayout(cam_header)

        self.video = VideoView()
        cam_lay.addWidget(self.video, 1)

        cam_info_bar = QHBoxLayout()
        cam_info_bar.setContentsMargins(14, 6, 14, 8)
        self.cam_resolution = QLabel("Resolution: -")
        self.cam_resolution.setObjectName("cameraInfo")
        self.cam_fps_label = QLabel("FPS: -")
        self.cam_fps_label.setObjectName("cameraInfo")
        self.cam_source_label = QLabel("Source: -")
        self.cam_source_label.setObjectName("cameraInfo")
        cam_info_bar.addWidget(self.cam_source_label)
        cam_info_bar.addStretch(1)
        cam_info_bar.addWidget(self.cam_resolution)
        cam_info_bar.addWidget(QLabel("  |  "))
        cam_info_bar.addWidget(self.cam_fps_label)
        cam_lay.addLayout(cam_info_bar)

        body.addWidget(cam_panel, 3)

        # Right column: System Status + Recent Activity
        right_col = QVBoxLayout()
        right_col.setSpacing(14)

        # System Status Panel
        status_panel = QFrame()
        status_panel.setObjectName("statusPanel")
        sp_lay = QVBoxLayout(status_panel)
        sp_lay.setContentsMargins(16, 14, 16, 14)
        sp_lay.setSpacing(10)
        sp_header = QHBoxLayout()
        sp_title = QLabel("System Status")
        sp_title.setObjectName("sectionTitle")
        sp_header.addWidget(sp_title)
        sp_header.addStretch(1)
        sp_lay.addLayout(sp_header)

        self.engine_status_label = QLabel("⏹ Stopped")
        self.engine_status_label.setObjectName("statusStopped")
        self.engine_status_detail = QLabel("Engine is not running")
        self.engine_status_detail.setObjectName("statusItem")
        sp_lay.addWidget(self.engine_status_label)
        sp_lay.addWidget(self.engine_status_detail)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #E2E8F0;")
        sp_lay.addWidget(sep)

        self._status_rows = {}
        for key, label in [("camera", "Camera"), ("database", "Database"),
                           ("recognition", "Recognition Engine"),
                           ("storage", "Storage"), ("recording", "Auto Recording")]:
            row = QHBoxLayout()
            dot = QLabel("●")
            dot.setObjectName("statusDot")
            dot.setStyleSheet("color: #94A3B8; font-size: 8px;")
            dot.setFixedWidth(16)
            lbl = QLabel(label)
            lbl.setObjectName("statusItem")
            status_text = QLabel("Idle")
            status_text.setObjectName("statusItem")
            status_text.setStyleSheet("color: #94A3B8;")
            status_text.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(dot)
            row.addWidget(lbl)
            row.addStretch(1)
            row.addWidget(status_text)
            sp_lay.addLayout(row)
            self._status_rows[key] = (dot, status_text)

        right_col.addWidget(status_panel)

        # Recent Activity
        activity_panel = QFrame()
        activity_panel.setObjectName("panel")
        ap_lay = QVBoxLayout(activity_panel)
        ap_lay.setContentsMargins(14, 12, 14, 12)
        ap_lay.setSpacing(8)
        ap_title = QLabel("Recent Activity")
        ap_title.setObjectName("sectionTitle")
        ap_lay.addWidget(ap_title)
        self.activity = QListWidget()
        self.activity.setAlternatingRowColors(True)
        self.activity.setStyleSheet("QListWidget { border: none; }")
        ap_lay.addWidget(self.activity, 1)
        right_col.addWidget(activity_panel, 1)

        body.addLayout(right_col, 2)
        root.addLayout(body, 1)

        # ── Control buttons row ─────────────────────────────────────
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.start_button = QPushButton("▶  Start Engine")
        self.start_button.setObjectName("success")
        self.pause_button = QPushButton("⏸  Pause")
        self.restart_button = QPushButton("↻  Restart")
        self.snapshot_button = QPushButton("📷  Snapshot")
        self.captures_button = QPushButton("📂  Open Folder")
        for btn in (self.start_button, self.pause_button, self.restart_button,
                    self.snapshot_button, self.captures_button):
            controls.addWidget(btn)
        controls.addStretch(1)
        root.addLayout(controls)

        # ── System Logs panel ───────────────────────────────────────
        log_panel = QFrame()
        log_panel.setObjectName("logPanel")
        lp_lay = QVBoxLayout(log_panel)
        lp_lay.setContentsMargins(14, 10, 14, 10)
        lp_lay.setSpacing(6)

        log_header = QHBoxLayout()
        log_title = QLabel("System Logs")
        log_title.setObjectName("sectionTitle")
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        self.autoscroll_check = QCheckBox("Auto Scroll")
        self.autoscroll_check.setChecked(True)
        log_header.addWidget(self.autoscroll_check)
        clear_btn = QPushButton("Clear")
        clear_btn.setObjectName("iconButton")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(lambda: self.log_list.clear())
        log_header.addWidget(clear_btn)
        lp_lay.addLayout(log_header)

        self.log_list = QListWidget()
        self.log_list.setFixedHeight(120)
        self.log_list.setStyleSheet(
            "QListWidget { border: none; font-family: 'SF Mono', 'Consolas', monospace; font-size: 12px; }")
        lp_lay.addWidget(self.log_list)

        root.addWidget(log_panel)

        # Toast
        self.toast = ToastBar()
        root.addWidget(self.toast)

        # Problem area (hidden by default, shown on errors)
        self.problem = QLabel("")
        self.problem.setObjectName("screenSubtitle")
        self.problem.setWordWrap(True)
        self.problem.hide()
        root.addWidget(self.problem)

        self._update_subtitle()

    def _connect(self):
        self.engine.frameReady.connect(self.on_frame)
        self.engine.statsReady.connect(self.on_stats)
        self.engine.captureTriggered.connect(self.on_capture)
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

    # ── engine control ───────────────────────────────────────────────

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
            self.toast.show_message("No camera frame yet — start the engine first.", "warn")
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

    # ── slots ────────────────────────────────────────────────────────

    def on_frame(self, frame):
        self.video.set_frame(frame)

    def on_stats(self, stats):
        # Stat cards
        faces = stats.get("faces", 0)
        self.face_count_label.setText(f"{faces} face{'s' if faces != 1 else ''}")
        self.cards["enrolled"].set_value(stats.get("enrolled", 0))
        self.cards["enrolled"].set_hint(describe_source(self.settings.source))
        known = stats.get("known_faces", 0)
        self.cards["verified"].set_value(known, "ok" if known else "idle")
        presence = stats.get("presence", 0.0)
        target = max(0.1, float(stats.get("capture_after", 3.0)))
        self.cards["waiting"].set_value(
            f"{presence:.1f}s",
            "warn" if 0 < presence < target else "ok" if presence >= target else "idle")
        self.cards["waiting"].set_hint(f"Need {target:.1f}s verified presence")
        self.cards["unknown"].set_value(stats.get("unknown_faces", 0),
                                        "bad" if stats.get("unknown_faces") else "idle")

        # Camera info bar
        status = stats.get("status", "STOPPED")
        self.camera_pill.set_status(status, STATUS_TONES.get(status, "idle"))
        self.cam_fps_label.setText(f"FPS: {stats.get('camera_fps', 0):.1f}")
        self.cam_source_label.setText(f"Source: {stats.get('source', '-')}")

        # Live badge visibility
        connected = status == "CONNECTED"
        self.live_badge.setVisible(connected)

        # System status rows
        self._set_status_row("camera", "Connected" if connected else status.title(),
                             connected)
        self._set_status_row("database", "Ready" if stats.get("storage_ready", False)
                             else "Not Ready", stats.get("storage_ready", False))
        self._set_status_row("recognition", "Active" if self.engine.running else "Idle",
                             self.engine.running)
        queue = stats.get("queue", 0)
        queue_max = max(1, stats.get("queue_max", 1))
        self._set_status_row("storage", f"{queue}/{queue_max} queued",
                             queue < queue_max)
        self._set_status_row("recording", "Paused" if self.engine.paused else
                             ("Recording" if self.engine.running else "Off"),
                             self.engine.running and not self.engine.paused)

        # Problems
        problems = [t for t in (stats.get("storage_error", ""),
                                stats.get("recognition_error", "")) if t]
        if not stats.get("storage_ready", False):
            problems.insert(0, "Attendance storage is not ready; recording is disabled.")
        if problems:
            self.problem.setText(" | ".join(problems))
            self.problem.show()
        else:
            self.problem.hide()

    def _set_status_row(self, key, text, ok):
        dot, label = self._status_rows[key]
        color = "#16A34A" if ok else "#DC2626"
        dot.setStyleSheet(f"color: {color}; font-size: 8px;")
        label.setText(text)
        label.setStyleSheet(f"color: {'#1E293B' if ok else '#DC2626'}; font-size: 12px;")

    def on_capture(self, job):
        self._log("INFO", f"CAPTURED  {job.employee_name} ({job.employee_id}), "
                  f"{job.duration:.1f}s verified")
        self.toast.show_message(
            f"Captured {job.employee_name} — saving attendance...", "warn")
        self._sound.play()

    def on_saved(self, result):
        recorded = datetime.fromtimestamp(result.recorded_at or 0).strftime("%H:%M:%S")
        self._log("INFO", f"{result.job.employee_name} ({result.job.employee_id}) "
                  f"recorded at {recorded}, {result.job.duration:.1f}s verified")
        self.toast.show_message(f"Attendance recorded for {result.job.employee_name}", "ok")
        self._add_activity(result.job.employee_name, "Verified",
                           f"Confidence: {result.job.duration:.1f}s presence")

    def on_failed(self, result):
        self._log("ERROR", f"Save failed: {result.error}")
        self.toast.show_message(f"Attendance save failed: {result.error}", "bad")

    def on_error(self, message):
        self._log("ERROR", str(message))
        self.toast.show_message(str(message), "bad")

    def on_log(self, message):
        self._log("INFO", str(message))

    def _sync_running(self, running):
        self.engine_pill.set_status("RUNNING" if running else "STOPPED",
                                    "ok" if running else "idle")
        self.start_button.setText("⏹  Stop Engine" if running else "▶  Start Engine")
        self.start_button.setObjectName("danger" if running else "success")
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)
        self.pause_button.setEnabled(bool(running))
        self._update_pause_button()

        if running:
            self.engine_status_label.setText("▶ Running")
            self.engine_status_label.setObjectName("statusRunning")
            self.engine_status_detail.setText("Engine is active and ready")
            self.video.show_message("Waiting for the camera",
                                    "The preview appears as soon as frames arrive")
        else:
            self.engine_status_label.setText("⏹ Stopped")
            self.engine_status_label.setObjectName("statusStopped")
            self.engine_status_detail.setText("Engine is not running")
            self.video.show_message("Engine stopped",
                                    "Press Start Engine to begin")
            self.camera_pill.set_status("STOPPED", "idle")
            self.face_count_label.setText("0 faces")
            for key in ("verified", "waiting", "unknown"):
                self.cards[key].set_value("0" if key != "waiting" else "-")
            for row_key in self._status_rows:
                self._set_status_row(row_key, "Idle", False)

        self.engine_status_label.style().unpolish(self.engine_status_label)
        self.engine_status_label.style().polish(self.engine_status_label)

    def _update_pause_button(self):
        paused = self.engine.paused
        self.pause_button.setText("▶  Resume" if paused else "⏸  Pause")
        self.pause_button.setObjectName("primary" if paused else "")
        self.pause_button.style().unpolish(self.pause_button)
        self.pause_button.style().polish(self.pause_button)
        if self.engine.running:
            self.engine_pill.set_status("PAUSED" if paused else "RUNNING",
                                        "warn" if paused else "ok")

    def _update_subtitle(self):
        self.subtitle.setText(f"Source: {describe_source(self.settings.source)}  •  "
                              f"Database: {self.settings.db_path}")

    # ── logging ──────────────────────────────────────────────────────

    def _log(self, level, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"[{level}]  {stamp}  {message}")
        self.log_list.addItem(item)
        while self.log_list.count() > 500:
            self.log_list.takeItem(0)
        if self.autoscroll_check.isChecked():
            self.log_list.scrollToBottom()

    def _add_activity(self, name, status, detail):
        stamp = datetime.now().strftime("%H:%M:%S")
        text = f"{stamp}  {name}  •  {status}  •  {detail}"
        self.activity.insertItem(0, QListWidgetItem(text))
        while self.activity.count() > 100:
            self.activity.takeItem(self.activity.count() - 1)

    # ── settings / lifecycle ─────────────────────────────────────────

    def on_settings_changed(self, settings):
        self.settings = settings
        self._sound = SoundPlayer(settings.alert_path, cooldown_sec=1.5)
        self._update_subtitle()

    def set_theme(self, theme):
        self.video.set_theme(theme)
        for card in self.cards.values():
            card.set_theme(theme)

    def close(self):
        self._sound.stop()
        super().close()
