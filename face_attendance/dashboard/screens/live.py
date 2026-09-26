"""Live Monitor: preview, engine control, live statistics, status and logs."""
from datetime import datetime
from dataclasses import replace
from pathlib import Path

import cv2
from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QMenu, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ...settings import describe_source
from ...config import ATTENDANCE_MODES
from ..icons import IconLabel, apply_button_icon, make_icon
from ..sound import SoundPlayer
from ..theme import palette, tone_color
from ..widgets import (ActivityFeed, Card, PageHeader, StatCard, StatusPill,
                       ToastBar, ToggleSwitch, VideoView)

STATUS_TONES = {"CONNECTED": "ok", "CONNECTING": "warn", "RECONNECTING": "warn",
                "CAMERA DISCONNECTED": "bad", "STOPPED": "idle"}

STATUS_ROW_ICONS = (("camera", "Camera"), ("database", "Database"),
                    ("shield", "Recognition Engine"), ("folder", "Storage"),
                    ("bolt", "Auto Recording"))

LOG_ROW_LIMIT = 250


class LogRow(QWidget):
    """One log line: level chip, monospace timestamp and message."""

    def __init__(self, level, stamp, message, theme="light", parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 1, 6, 1)
        row.setSpacing(10)
        self.tag = QLabel(level)
        self.tag.setObjectName("logTag")
        self.tag.setProperty("level", level)
        self.tag.setFixedWidth(52)
        self.tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.tag)
        self.stamp = QLabel(stamp)
        self.stamp.setObjectName("logTimestamp")
        row.addWidget(self.stamp)
        self.message = QLabel(message)
        self.message.setObjectName("logMessage")
        self.message.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.message, 1)

    def set_theme(self, theme):
        for widget in (self.tag, self.stamp, self.message):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class LiveScreen(QWidget):
    flashPreviewRequested = pyqtSignal()
    viewAllRequested = pyqtSignal()
    settingsRequested = pyqtSignal()
    settingsApplied = pyqtSignal(object, bool)

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.cards = {}
        self._status_rows = {}
        self._log_rows = []
        self._theme = settings.theme
        self._sound = SoundPlayer(settings.alert_path, cooldown_sec=1.5)
        self._build()
        self._connect()
        self._sync_running(engine.running)

    # ── build ────────────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("liveScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.main_scroll = scroll

        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(12)

        self.header = PageHeader("Live Monitor", "Keep your whole face visible and follow the camera prompts.")
        self.engine_pill = StatusPill("ENGINE STOPPED", "idle", dot=True)
        self.header.add_action(self.engine_pill)
        root.addWidget(self.header)

        # ── KPI cards ───────────────────────────────────────────────────
        cards_row = QHBoxLayout()
        cards_row.setSpacing(12)
        card_defs = (
            ("enrolled", "Enrolled", "-", "users", "blue", "Employees in catalog"),
            ("verified", "Verified Today", "0", "check-circle", "green", "Successful check-ins"),
            ("waiting", "Waiting", "-", "clock", "orange", "Presence accumulating"),
            ("unknown", "Unknown", "0", "alert", "purple", "Faces not in catalog"),
        )
        for key, label, value, icon, tone, hint in card_defs:
            card = StatCard(label, value, hint=hint, icon=icon, tone=tone)
            self.cards[key] = card
            cards_row.addWidget(card)
        root.addLayout(cards_row)

        # ── body: camera + logs (left) | status + activity (right) ──────
        body = QHBoxLayout()
        body.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(10)
        left.addLayout(self._build_controls_row())
        left.addWidget(self._build_camera_card(), 1)
        body.addLayout(left, 3)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._build_requirements_card())
        right.addWidget(self._build_status_card())
        right.addWidget(self._build_activity_card(), 1)
        body.addLayout(right, 2)

        root.addLayout(body, 1)
        root.addWidget(self._build_log_card())

        self.toast = ToastBar(theme=self._theme)
        root.addWidget(self.toast)

        self.problem = QLabel("")
        self.problem.setObjectName("emptyBody")
        self.problem.setWordWrap(True)
        self.problem.hide()
        root.addWidget(self.problem)

        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

    def _build_requirements_card(self):
        card = Card("Attendance requirements", "Choose one option", icon="shield", theme=self._theme)
        self.requirements_card = card
        self.requirement_group = QButtonGroup(self)
        self.requirement_group.setExclusive(True)
        self.requirement_boxes = {}
        for mode, label in ATTENDANCE_MODES.items():
            checkbox = QCheckBox(label)
            checkbox.setProperty("attendance_mode", mode)
            checkbox.setChecked(mode == self.settings.attendance_mode)
            self.requirement_group.addButton(checkbox)
            self.requirement_boxes[mode] = checkbox
            card.add(checkbox)
        self.requirement_note = QLabel()
        self.requirement_note.setWordWrap(True)
        card.add(self.requirement_note)
        self.apply_requirements_button = QPushButton("Apply requirements")
        self.apply_requirements_button.setToolTip("Save this option and restart a running engine to clear old verification evidence")
        self.apply_requirements_button.clicked.connect(self._apply_requirements)
        actions = QHBoxLayout()
        actions.addWidget(self.apply_requirements_button)
        self.test_sound_button = QPushButton("Test alert sound")
        self.test_sound_button.clicked.connect(self._test_sound)
        actions.addWidget(self.test_sound_button)
        card.add_layout(actions)
        self.requirement_group.buttonToggled.connect(lambda *_: self._update_requirements_note())
        self._sync_requirements()
        return card

    def _sync_requirements(self):
        self.requirement_boxes[self.settings.attendance_mode].setChecked(True)
        self._update_requirements_note()

    def _update_requirements_note(self):
        active = getattr(self.engine, "config", None)
        mode = active.attendance_mode if self.engine.running and active else self.settings.attendance_mode
        selected = self.requirement_group.checkedButton()
        pending = selected.property("attendance_mode") if selected else mode
        note = (f"Selected: {ATTENDANCE_MODES[pending]}. Click Apply requirements to activate. "
                if pending != mode else "")
        self.requirement_note.setText(
            note + f"{'Active' if self.engine.running else 'On next start'}: {ATTENDANCE_MODES[mode]}. "
            "Face recognition and anti-spoof checks remain required.")

    def _apply_requirements(self):
        mode = self.requirement_group.checkedButton().property("attendance_mode")
        candidate = replace(self.settings, attendance_mode=mode)
        try:
            candidate.to_config()
            candidate._path = candidate.save(getattr(self.settings, "_path", None))
        except (OSError, ValueError) as exc:
            self.toast.show_message(f"Could not save attendance requirements: {exc}", "bad")
            return
        running = self.engine.running
        self.settings = candidate
        self.engine.settings = candidate
        try:
            if running:
                self.engine.restart(candidate)
            self.settingsApplied.emit(candidate, running)
            self._sync_requirements()
            self.toast.show_message(f"Saved: {ATTENDANCE_MODES[mode]}", "ok")
        except (OSError, ValueError, RuntimeError) as exc:
            self.settingsApplied.emit(candidate, False)
            self._sync_requirements()
            self.toast.show_message(f"Requirements saved; engine restart failed: {exc}", "bad")

    def _test_sound(self):
        if not self._sound.available:
            self.toast.show_message("Alert sound unavailable. Check the WAV path in Settings.", "bad")
        elif not self._sound.play():
            self.toast.show_message("Sound is busy or playback failed; try again shortly.", "warn")

    def _build_camera_card(self):
        card = Card("Camera Preview", "", icon="camera", theme=self._theme)
        self.camera_card = card

        self.live_badge = QLabel("LIVE")
        self.live_badge.setObjectName("liveBadge")
        self.live_badge.setFixedHeight(22)
        card.actions.addWidget(self.live_badge)

        self.gear_button = QPushButton()
        self.gear_button.setObjectName("iconButton")
        self.gear_button.setFixedSize(34, 34)
        self.gear_button.setToolTip("Camera settings")
        self.gear_button.clicked.connect(self.settingsRequested.emit)
        card.actions.addWidget(self.gear_button)

        self.expand_button = QPushButton()
        self.expand_button.setObjectName("iconButton")
        self.expand_button.setFixedSize(34, 34)
        self.expand_button.setToolTip("Toggle the on-screen picture guide")
        self.expand_button.clicked.connect(self._toggle_guide)
        card.actions.addWidget(self.expand_button)

        self.camera_box = QComboBox()
        self.camera_box.addItem(describe_source(self.settings.source))
        self.camera_box.setEnabled(False)
        self.camera_box.setFixedWidth(170)
        self.camera_box.setToolTip("Change the source on the Settings screen")
        card.actions.addWidget(self.camera_box)

        self.video = VideoView(theme=self._theme, message="Position your face in the frame",
                               subtitle="The engine is running and ready to detect")
        self.video.setMinimumHeight(240)
        frame = QFrame()
        frame.setObjectName("cameraFrame")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        frame_layout.addWidget(self.video)
        card.add(frame, 1)

        info = QHBoxLayout()
        info.setSpacing(14)
        self.cam_source_label = QLabel("Source: -")
        self.cam_source_label.setObjectName("cameraValue")
        self.cam_resolution = QLabel("Resolution: -")
        self.cam_resolution.setObjectName("cameraInfo")
        self.cam_fps_label = QLabel("FPS: -")
        self.cam_fps_label.setObjectName("cameraInfo")
        self.cam_pipeline = QLabel("Pipeline: idle")
        self.cam_pipeline.setObjectName("cameraInfo")
        info.addWidget(self.cam_source_label)
        info.addStretch(1)
        info.addWidget(self.cam_pipeline)
        info.addWidget(self.cam_resolution)
        info.addWidget(self.cam_fps_label)
        card.add_layout(info)
        return card

    def _build_controls_row(self):
        row = QHBoxLayout()
        row.setSpacing(6)
        self.start_button = QPushButton("Start Engine")
        self.start_button.setObjectName("controlButtonSuccess")
        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("controlButton")
        self.restart_button = QPushButton("Restart")
        self.restart_button.setObjectName("controlButton")
        self.snapshot_button = QPushButton("Snapshot")
        self.snapshot_button.setObjectName("controlButton")
        self.captures_button = QPushButton("Captures")
        self.captures_button.setObjectName("controlButton")
        self.more_button = QPushButton("More")
        self.more_button.setObjectName("controlButton")
        menu = QMenu(self)
        menu.addAction("Open captures folder", self._open_captures)
        menu.addAction("Restart the engine", self._restart)
        menu.addAction("Camera settings", self.settingsRequested.emit)
        menu.addAction("Preview capture flash", self.flashPreviewRequested.emit)
        self.more_button.setMenu(menu)
        for button in (self.start_button, self.pause_button, self.restart_button,
                       self.snapshot_button, self.captures_button, self.more_button):
            button.setMinimumHeight(34)
            row.addWidget(button)
        row.addStretch(1)
        return row

    def _build_log_card(self):
        card = Card("System Logs", "", icon="list", compact=True, theme=self._theme)
        self.log_card = card
        self.auto_toggle = ToggleSwitch(True, theme=self._theme)
        self.auto_toggle.setToolTip("Scroll to the newest entry automatically")
        card.actions.addWidget(self.auto_toggle)
        self.log_toggle = QPushButton("Hide")
        self.log_toggle.setObjectName("ghostButton")
        self.log_toggle.clicked.connect(self._toggle_logs)
        card.actions.addWidget(self.log_toggle)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setObjectName("iconButtonFlat")
        self.clear_button.clicked.connect(self.clear_logs)
        card.actions.addWidget(self.clear_button)

        self.log_list = QListWidget()
        self.log_list.setObjectName("logList")
        self.log_list.setFixedHeight(95)
        card.add(self.log_list)
        return card

    def _build_status_card(self):
        card = Card("System Status", "", icon="shield", theme=self._theme)
        self.status_card = card
        card.setMinimumHeight(220)

        self.banner = QFrame()
        self.banner.setObjectName("banner")
        self.banner.setMinimumHeight(50)
        banner_row = QHBoxLayout(self.banner)
        banner_row.setContentsMargins(12, 8, 12, 8)
        banner_row.setSpacing(10)
        self._banner_icon = IconLabel("check-circle", 20)
        banner_row.addWidget(self._banner_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        banner_text = QVBoxLayout()
        banner_text.setSpacing(1)
        self.engine_status_label = QLabel("Stopped")
        self.engine_status_label.setObjectName("bannerTitle")
        self.engine_status_detail = QLabel("Engine is not running")
        self.engine_status_detail.setObjectName("bannerBody")
        banner_text.addWidget(self.engine_status_label)
        banner_text.addWidget(self.engine_status_detail)
        banner_row.addLayout(banner_text)
        banner_row.addStretch(1)
        card.add(self.banner)

        for key, (icon_name, label) in zip(("camera", "database", "recognition",
                                            "storage", "recording"), STATUS_ROW_ICONS):
            row_frame = QFrame()
            row_frame.setObjectName("statusRow")
            row_frame.setMinimumHeight(24)
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(6, 3, 6, 3)
            row.setSpacing(10)
            icon = IconLabel(icon_name, 16)
            row.addWidget(icon)
            name = QLabel(label)
            name.setObjectName("statusItem")
            row.addWidget(name)
            row.addStretch(1)
            value = QLabel("Idle")
            value.setObjectName("statusValue")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            row.addWidget(value)
            dot = IconLabel("dot", 10)
            row.addWidget(dot)
            card.add(row_frame)
            self._status_rows[key] = (dot, value, icon)
        return card

    def _build_activity_card(self):
        card = Card("Recent Activity", "", icon="list", theme=self._theme)
        self.activity_card = card
        card.setMinimumHeight(150)
        self.view_all_button = QPushButton("View all")
        self.view_all_button.setObjectName("linkButton")
        self.view_all_button.clicked.connect(self.viewAllRequested.emit)
        card.actions.addWidget(self.view_all_button)

        self.activity = ActivityFeed(theme=self._theme, max_rows=8)
        scroll = QScrollArea()
        scroll.setObjectName("scrollFeed")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(100)
        scroll.setWidget(self.activity)
        self.activity_scroll = scroll
        card.add(scroll, 1)
        return card

    # ── wiring ───────────────────────────────────────────────────────────

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

    # ── engine control ───────────────────────────────────────────────────

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

    def _toggle_guide(self):
        self.video.set_guide(not self.video._guide)

    def _toggle_logs(self):
        visible = not self.log_list.isVisible()
        self.log_list.setVisible(visible)
        self.log_toggle.setText("Hide" if visible else "Show")

    def _save_snapshot(self):
        frame = self.engine.grab_clean_frame()
        if frame is None:
            self.toast.show_message("No camera frame yet - start the engine first.", "warn")
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

    def clear_logs(self):
        self.log_list.clear()
        self._log_rows = []

    # ── slots ────────────────────────────────────────────────────────────

    def on_frame(self, frame):
        self.video.set_frame(frame)

    def on_stats(self, stats):
        # KPI cards
        faces = stats.get("faces", 0)
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
        unknown = stats.get("unknown_faces", 0)
        self.cards["unknown"].set_value(unknown, "bad" if unknown else "idle")

        # Camera strip + overlay chips
        status = stats.get("status", "STOPPED")
        fps = stats.get("camera_fps", 0)
        self.cam_fps_label.setText(f"FPS: {fps:.1f}")
        self.cam_source_label.setText(f"Source: {stats.get('source', '-')}")
        resolution = stats.get("resolution") or stats.get("resolution_text")
        if resolution:
            self.cam_resolution.setText(f"Resolution: {resolution}")
            self.video.set_overlay(resolution=resolution, fps=fps, faces=faces)
        else:
            width, height = stats.get("width"), stats.get("height")
            if width and height:
                self.cam_resolution.setText(f"Resolution: {int(width)} x {int(height)}")
                self.video.set_overlay(resolution=f"{int(width)} x {int(height)}",
                                       fps=fps, faces=faces)
            else:
                self.video.set_overlay(fps=fps, faces=faces)
        queue = stats.get("queue", 0)
        queue_max = max(1, stats.get("queue_max", 1))
        self.cam_pipeline.setText(f"Pipeline: {queue}/{queue_max} queued")

        # Live badge visibility
        connected = status == "CONNECTED"
        self.live_badge.setVisible(connected)

        # System status rows
        self._set_status_row("camera", "Connected" if connected else status.title(), connected)
        self._set_status_row("database", "Ready" if stats.get("storage_ready", False)
                             else "Not Ready", stats.get("storage_ready", False))
        self._set_status_row("recognition", "Running" if self.engine.running else "Idle",
                             self.engine.running)
        self._set_status_row("storage", f"{queue}/{queue_max} queued", queue < queue_max)
        self._set_status_row("recording", "Paused" if self.engine.paused else
                             ("Enabled" if self.engine.running else "Off"),
                             self.engine.running and not self.engine.paused)

        # Problems
        problems = [t for t in (stats.get("storage_error", ""),
                                stats.get("recognition_error", "")) if t]
        if self.engine.running:
            self.engine_status_detail.setText(
                "Face recognized; liveness not verified" if stats.get("liveness_blocked")
                else "Engine is active and ready to detect")
        self.engine_status_detail.setToolTip(
            ("Turn off Portrait/background blur or beauty filters if enabled. "
             "Keep your whole face visible in even light.\n" + stats.get("liveness_details", ""))
            if stats.get("liveness_blocked") else "")
        if not stats.get("storage_ready", False):
            problems.insert(0, "Attendance storage is not ready; recording is disabled.")
        if problems:
            self.problem.setText(" | ".join(problems))
            self.problem.show()
        else:
            self.problem.hide()

    def _set_status_row(self, key, text, ok):
        dot, value, _icon = self._status_rows[key]
        dot.set_icon_color(tone_color(self._theme, "ok" if ok else "bad"))
        value.setText(text)
        value.setStyleSheet(
            f"color: {tone_color(self._theme, 'ok' if ok else 'bad')}; font-weight: 600;")

    # ── events ───────────────────────────────────────────────────────────

    def on_capture(self, job):
        self._log("INFO", f"CAPTURED  {job.employee_name} ({job.employee_id}), "
                  f"{job.duration:.1f}s verified")
        self.toast.show_message(
            f"Captured {job.employee_name} \u2014 saving attendance...", "warn")

    def on_saved(self, result):
        self._sound.play()
        recorded = datetime.fromtimestamp(result.recorded_at or 0).strftime("%H:%M:%S")
        self._log("INFO", f"{result.job.employee_name} ({result.job.employee_id}) "
                  f"recorded at {recorded}, {result.job.duration:.1f}s verified")
        self.toast.show_message(f"Attendance recorded for {result.job.employee_name}", "ok")
        self._add_activity(result.job.employee_name, "Verified",
                           f"{result.job.duration:.1f}s verified presence",
                           result.job.employee_id)

    def on_failed(self, result):
        self._log("ERROR", f"Save failed: {result.error}")
        self.toast.show_message(f"Attendance save failed: {result.error}", "bad")

    def on_error(self, message):
        self._log("ERROR", str(message))
        self.toast.show_message(str(message), "bad")

    def on_log(self, message):
        self._log("INFO", str(message))

    # ── state sync ───────────────────────────────────────────────────────

    def _sync_running(self, running):
        self._sync_requirements()
        self.engine_pill.set_status("ENGINE RUNNING" if running else "ENGINE STOPPED",
                                    "ok" if running else "idle")
        self.pause_button.setEnabled(bool(running))
        self._update_pause_button(retint=False)

        if running:
            self.start_button.setText("Stop Engine")
            self.start_button.setObjectName("controlButtonDanger")
            self.engine_status_label.setText("Running")
            self.engine_status_detail.setText("Engine is active and ready to detect")
            self.banner.setProperty("tone", "ok")
            self._banner_icon.set_icon("check-circle")
            self._banner_icon.set_icon_color(tone_color(self._theme, "ok"))
            self.video.show_message("Position your face in the frame",
                                    "The engine is watching for enrolled employees")
        else:
            self.start_button.setText("Start Engine")
            self.start_button.setObjectName("controlButtonSuccess")
            self.engine_status_label.setText("Stopped")
            self.engine_status_detail.setText("Engine is not running")
            self.banner.setProperty("tone", "idle")
            self._banner_icon.set_icon("alert")
            self._banner_icon.set_icon_color(tone_color(self._theme, "muted"))
            self.video.show_message("Engine stopped", "Press Start Engine to begin")
            self.cards["verified"].set_value("0", "idle")
            self.cards["waiting"].set_value("-")
            self.cards["unknown"].set_value("0", "idle")
            self.video.set_overlay(faces=0)
            for row_key in self._status_rows:
                self._set_status_row(row_key, "Idle", True)
            self.live_badge.setVisible(False)
        self.banner.style().unpolish(self.banner)
        self.banner.style().polish(self.banner)
        self.start_button.style().unpolish(self.start_button)
        self.start_button.style().polish(self.start_button)
        self._retint_buttons(running=running)

    def _update_pause_button(self, retint=True):
        paused = self.engine.paused
        self.pause_button.setText("Resume" if paused else "Pause")
        self.pause_button.setObjectName("softButton" if paused else "controlButton")
        self.pause_button.style().unpolish(self.pause_button)
        self.pause_button.style().polish(self.pause_button)
        if retint:
            self._retint_buttons()
        if self.engine.running:
            self.engine_pill.set_status("ENGINE PAUSED" if paused else "ENGINE RUNNING",
                                        "warn" if paused else "ok")

    def _retint_buttons(self, running=None):
        """Buttons carry vector glyphs, so re-colour them when state or theme changes."""
        colors = palette(self._theme)
        if running is None:
            running = self.engine.running
        plan = (
            (self.start_button, "stop" if running else "play"),
            (self.pause_button, "play" if self.engine.paused else "pause"),
            (self.restart_button, "restart"),
            (self.snapshot_button, "image"),
            (self.captures_button, "folder"),
            (self.more_button, "more"),
            (self.gear_button, "gear"),
            (self.expand_button, "expand"),
            (self.clear_button, "trash"),
        )
        for button, name in plan:
            object_name = button.objectName()
            if object_name in ("primary", "success", "controlButtonSuccess"):
                color = "#FFFFFF"
            elif object_name in ("danger", "controlButtonDanger"):
                color = colors["danger"]
            elif object_name in ("iconButton", "iconButtonFlat", "ghostButton"):
                color = colors["muted"]
            else:
                color = colors["text_secondary"]
            apply_button_icon(button, name, color, size=14)
        self.start_button.setText("Stop Engine" if running else "Start Engine")

    def _update_subtitle(self):
        self.header.set_subtitle(
            f"Source: {describe_source(self.settings.source)}")

    # ── logging ──────────────────────────────────────────────────────────

    def _log(self, level, message):
        stamp = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem()
        row = LogRow(level, stamp, message, theme=self._theme)
        self.log_list.addItem(item)
        self.log_list.setItemWidget(item, row)
        self._log_rows.append(row)
        while self.log_list.count() > LOG_ROW_LIMIT:
            self.log_list.takeItem(0)
            if self._log_rows:
                self._log_rows.pop(0)
        if self.auto_toggle.isChecked():
            self.log_list.scrollToBottom()

    def _add_activity(self, name, status, detail, employee_id=""):
        self.activity.add(
            name,
            detail=detail,
            status=status,
            tone="ok" if status == "Verified" else "bad",
            time_text=datetime.now().strftime("%H:%M:%S"),
            subtitle=employee_id,
        )

    # ── settings / lifecycle ─────────────────────────────────────────────

    def on_settings_changed(self, settings):
        self.settings = settings
        self._sound.stop()
        self._sound = SoundPlayer(settings.alert_path, cooldown_sec=1.5)
        self._sync_requirements()
        if self.camera_box.count():
            self.camera_box.setItemText(0, describe_source(settings.source))
        self._update_subtitle()

    def set_theme(self, theme):
        self._theme = theme
        self.video.set_theme(theme)
        self.toast.set_theme(theme)
        self.auto_toggle.set_theme(theme)
        self.activity.set_theme(theme)
        for card in self.cards.values():
            card.set_theme(theme)
        for panel in (self.camera_card, self.log_card, self.status_card, self.activity_card,
                      self.requirements_card):
            panel.set_theme(theme)
        self.engine_pill.set_status(self.engine_pill.label.text(),
                                    self.engine_pill.property("tone") or "idle")
        for dot, _value, icon in self._status_rows.values():
            icon.set_icon_color(palette(theme)["muted"])
        for row in self._log_rows:
            row.set_theme(theme)
        self._retint_buttons()

    def close(self):
        self._sound.stop()
        super().close()
