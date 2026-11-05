"""Live license plate screen; camera reads, inference and disk writes stay off Qt."""
from dataclasses import replace
from pathlib import Path
import threading
import time

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QKeySequence, QShortcut, QDesktopServices
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QDoubleSpinBox, QSpinBox, QFrame,
                             QScrollArea, QSizePolicy, QHeaderView,
                             QFormLayout, QTableWidget, QTableWidgetItem, QMenu)

from face_attendance.camera import CameraManager
from face_attendance.power import SleepInhibitor
from face_attendance.config import ROOT
from face_attendance.settings import describe_source
from face_attendance.dashboard.access import guard
from face_attendance.dashboard.bridge import to_pixmap
from face_attendance.dashboard.threads import settle
from face_attendance.dashboard.icons import apply_button_icon
from face_attendance.dashboard.theme import palette
from face_attendance.dashboard.widgets import Card, StatCard, StatusPill, ToggleSwitch, VideoView
from .core import CapturePolicy, PlateTracker, load_captures, create_recognizer, read_plates, save_capture
from .maintenance import demo_fleet, load_fleet, save_fleet, plate_key, maintenance_status


class PlateWorker(QThread):
    result = pyqtSignal(object)
    saved = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, config, directory, threshold, confirmations, cooldown,
                 interval, shared=None, recognizer_factory=create_recognizer,
                 capture_factory=None):
        super().__init__()
        self.config, self.directory = config, directory
        self.threshold, self.interval = threshold, interval
        self.policy = CapturePolicy(confirmations, cooldown)
        self.tracker = PlateTracker()
        self.shared, self.recognizer_factory = shared, recognizer_factory
        self.camera = None if shared else CameraManager(config, capture_factory)
        self.automatic = threading.Event()
        self.manual = threading.Event()
        self._preview_active = threading.Event()
        self._sleep_inhibitor = SleepInhibitor()
        self.inference_ms = 0.0

    def preview_packet(self):
        """Read the camera mailbox directly so slow OCR cannot freeze video."""
        if not self._preview_active.is_set():
            return None
        packet = self.shared() if self.shared else self.camera.frames.get()
        if packet is None or time.monotonic() - packet.captured_at > self.config.camera_stale_sec:
            return None
        return packet

    @property
    def latest(self):
        packet = self.preview_packet()
        return None if packet is None else packet.frame

    def stop(self):
        self._preview_active.clear()
        self.requestInterruption()
        if self.camera:
            self.camera.stop_event.set()
        # Inference may be finishing; retain the worker without freezing Qt.
        settle(self, timeout_ms=100)

    def run(self):
        recognizer = None
        try:
            recognizer = self.recognizer_factory()
            if self.isInterruptionRequested():
                return
            if self.camera:
                self.camera.start()
            self._sleep_inhibitor.acquire()
            self._preview_active.set()
            next_read, last_identity = 0, None
            last_generation = None
            had_result = False
            while not self.isInterruptionRequested():
                now = time.monotonic()
                packet = self.preview_packet()
                if packet is None:
                    self.policy.counts.clear()
                    if had_result:
                        self.result.emit([])
                        had_result = False
                    self.msleep(20)
                    continue
                generation = getattr(packet, "generation", 0)
                identity = (generation, packet.sequence)
                if generation != last_generation:
                    self.policy.counts.clear()
                    self.tracker.active.clear()
                    last_generation = generation
                if identity == last_identity:
                    self.msleep(20)
                    continue
                last_identity = identity
                frame = packet.frame
                if now < next_read:
                    self.msleep(20)
                    continue
                next_read = now + self.interval
                inference_started = time.monotonic()
                plates = read_plates(recognizer, frame, self.threshold)
                self.inference_ms = (time.monotonic() - inference_started) * 1000
                if self.isInterruptionRequested():
                    break
                now = time.monotonic()
                current = self.preview_packet()
                if (current is None
                        or now - packet.captured_at > self.config.camera_stale_sec
                        or getattr(current, "generation", 0) != generation):
                    # Never confirm or save a result from a disconnected camera
                    # or an inference that has outlived the frame freshness limit.
                    self.policy.counts.clear()
                    self.result.emit([])
                    had_result = False
                    continue
                plates = self.tracker.update(plates, now)
                eligible = self.policy.eligible(plates, now)
                manual = self.manual.is_set()
                self.manual.clear()
                candidates = plates if manual else eligible if self.automatic.is_set() else []
                for plate in {p.text: p for p in candidates}.values():
                    record = save_capture(self.directory, frame, plate, self.config.source, not manual)
                    self.policy.mark_saved(plate, now)
                    self.saved.emit(record)
                self.result.emit(plates)
                had_result = True
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self._sleep_inhibitor.release()
            self._preview_active.clear()
            try:
                close = getattr(recognizer, 'close', None)
                if close is not None:
                    close()
            except Exception as exc:
                self.error.emit(f'Could not close plate OCR: {exc}')
            finally:
                if self.camera:
                    self.camera.close()


class LicensePlateScreen(QWidget):
    detachRequested = pyqtSignal()
    def __init__(self, engine, settings):
        super().__init__()
        self.engine, self.settings = engine, settings
        self.worker = None
        self.plates = []
        self._theme = settings.theme
        self._capture_count = 0
        self._unique_plates = set()
        self._unique_plate_overflow = False
        self._error = False
        self._fs_active = False
        self._can_control = self._can_capture = True
        self._last_preview_key = None
        self._reading_at = 0
        self._restart_pending = False
        self._stopping = False
        self.directory = ROOT / "plate_captures"
        self.fleet_path = ROOT / "plate_maintenance.json"
        self.fleet = []
        self._maintenance_counts = {}
        self._build()
        self._load_fleet()
        self._load_history()
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.capture_button.clicked.connect(self.capture)
        self.auto.toggled.connect(self._automatic)
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self._preview)
        engine.runningChanged.connect(self._engine_changed)
        self.escape_shortcut = QShortcut(QKeySequence("Escape"), self)
        self.escape_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.escape_shortcut.activated.connect(self._exit_fullscreen)
        self.escape_shortcut.setEnabled(False)
        self._controls(False)

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.main_scroll = QScrollArea()
        self.main_scroll.setObjectName("liveScroll")
        self.main_scroll.setWidgetResizable(True)
        self.main_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.main_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(20, 14, 20, 14)
        root.setSpacing(12)
        self.surfaces = []

        body = QHBoxLayout()
        body.setSpacing(12)
        camera = self._card("Live Camera", "Keep the license plate clear and fully visible", "camera")
        self.camera_card = camera
        self.camera_pill = StatusPill("STOPPED", "idle", dot=True, theme=self._theme)
        camera.actions.addWidget(self.camera_pill)
        self.preview = VideoView(theme=self._theme, message="Ready to read license plates",
                                 subtitle="Start the camera to begin recognition")
        self.preview.set_guide(False)
        self.preview.setMinimumHeight(520)
        self.preview_hud = self.preview.enable_controls()
        self.preview_hud.set_source(describe_source(self.settings.source))
        self.preview_hud.record.clicked.connect(self._toggle_camera)
        self.preview_hud.capture.clicked.connect(self.capture)
        self.preview_hud.snapshot.clicked.connect(self.capture)
        self.preview_hud.pause.clicked.connect(lambda: self.auto.setChecked(not self.auto.isChecked()))
        self.preview_hud.restart.clicked.connect(self._restart)
        self.preview_hud.settings.clicked.connect(self._open_settings)
        self.preview_hud.captures.clicked.connect(self._open_captures)
        self.preview_hud.fullscreen.clicked.connect(self._toggle_fullscreen)
        self.preview_hud.detach.clicked.connect(self.detachRequested.emit)
        self.preview_menu = QMenu(self)
        self.preview_menu.addAction("Open captures folder", self._open_captures)
        self.preview_menu.addAction("Camera and capture settings", self._open_settings)
        self.fullscreen_action = self.preview_menu.addAction("Enter full screen", self._toggle_fullscreen)
        self.detach_action = self.preview_menu.addAction("Open in new window", self.detachRequested.emit)
        self.preview_hud.more.setMenu(self.preview_menu)
        self.preview_hud.more.setPopupMode(self.preview_hud.more.ToolButtonPopupMode.InstantPopup)
        frame = QFrame()
        frame.setObjectName("cameraFrame")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        frame_layout.addWidget(self.preview)
        camera.add(frame, 1)
        info = QHBoxLayout()
        self.source_label = QLabel("Camera " + str(self.settings.source))
        self.source_label.setObjectName("cameraValue")
        self.source_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.resolution_label = QLabel("Resolution —")
        self.resolution_label.setObjectName("cameraInfo")
        self.mode_label = QLabel("Manual capture")
        self.mode_label.setObjectName("cameraInfo")
        self.latency_label = QLabel("Analysis —")
        self.latency_label.setObjectName("cameraInfo")
        info.addWidget(self.source_label, 1)
        info.addWidget(self.resolution_label)
        info.addWidget(self.latency_label)
        info.addWidget(self.mode_label)
        camera.add_layout(info)

        self.controls_panel = QWidget()
        controls = QHBoxLayout(self.controls_panel)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self.start_button = QPushButton("Start camera")
        self.start_button.setObjectName("controlButtonSuccess")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("controlButtonDanger")
        self.capture_button = QPushButton("Capture plate")
        self.capture_button.setObjectName("controlButton")
        self.capture_button.setToolTip("Read and save plates on the next camera reading")
        for button in (self.start_button, self.stop_button, self.capture_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            controls.addWidget(button)
        controls.addStretch(1)
        self.auto_label = QLabel("Auto capture")
        self.auto_label.setObjectName("cameraInfo")
        self.auto = ToggleSwitch(theme=self._theme)
        self.auto.setAccessibleName("Automatic plate capture")
        self.auto.setToolTip("Capture after matching readings; suppress repeats during cooldown")
        controls.addWidget(self.auto_label)
        controls.addWidget(self.auto)
        self.fullscreen_button = QPushButton("Full Screen")
        self.fullscreen_button.setObjectName("controlButton")
        self.fullscreen_button.clicked.connect(self._toggle_fullscreen)
        controls.addWidget(self.fullscreen_button)
        camera.add(self.controls_panel)
        # Actions remain available to access control and tests; the shared HUD
        # is the visible control surface, as in Live Monitor.
        self.controls_panel.hide()
        self.status = QLabel("Start the camera. Capture manually or turn on auto capture.")
        self.status.setObjectName("cardSubtitle")
        self.status.setWordWrap(True)
        camera.add(self.status)
        body.addWidget(camera, 7)

        details = self._card("Plate Details", "Latest recognition", "shield")
        self.details_card = details
        details.setMinimumWidth(260)
        self.plate_pill = StatusPill("WAITING", "idle", dot=True, theme=self._theme)
        details.add(self.plate_pill)
        self.plate_text = QLabel("—")
        self.plate_text.setObjectName("statValue")
        self.plate_text.setWordWrap(True)
        details.add(self.plate_text)
        self.confidence_label = self._detail("Confidence", "—", details)
        self.detected_label = self._detail("Plates in view", "0", details)
        self.last_capture_label = self._detail("Last capture", "No captures yet", details)
        self.capture_type_label = self._detail("Capture mode", "Manual", details)
        self.crop_preview = QLabel("Plate crop appears after capture")
        self.crop_preview.setObjectName("subtlePanel")
        self.crop_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop_preview.setWordWrap(True)
        self.crop_preview.setFixedHeight(90)
        self.crop_preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        details.add(self.crop_preview)
        recent = QLabel("Recent captures")
        recent.setObjectName("cardTitle")
        details.add(recent)
        self.history = QTableWidget(0, 5)
        self.history.setHorizontalHeaderLabels(["Time (UTC)", "Plate", "Score", "Image", "Track"])
        self.history.itemSelectionChanged.connect(self._select_capture)
        self.history.setToolTip("Select a capture to inspect its saved plate image")
        self.history.verticalHeader().hide()
        self.history.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history.setShowGrid(False)
        self.history.setAlternatingRowColors(True)
        self.history.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history.setMinimumHeight(120)
        self.history.setMaximumHeight(170)
        details.add(self.history, 1)
        self.empty_history = QLabel("Saved plates will appear here.")
        self.empty_history.setObjectName("emptyBody")
        details.add(self.empty_history)
        body.addWidget(details, 3)
        root.addLayout(body)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.cards = {}
        for key, title, value, hint, icon, tone in (
                ("detected", "In View", "0", "Plates above the confidence limit", "camera", "blue"),
                ("captured", "Captured", "0", "Saved in this session", "check-circle", "green"),
                ("unique", "Unique Plates", "0", "Different plates captured", "list", "purple"),
                ("mode", "Capture Mode", "Manual", "Save with Capture plate", "bolt", "orange")):
            card = StatCard(title, value, hint=hint, icon=icon, tone=tone, theme=self._theme)
            self.cards[key] = card
            metrics.addWidget(card)
        root.addLayout(metrics)
        fleet_card = self._card("Truck Maintenance", "Exact plate match • date based service • demo data only", "shield")
        self.fleet_card = fleet_card
        self.maintenance_pill = StatusPill("WAITING", "idle", dot=True, theme=self._theme)
        fleet_card.add(self.maintenance_pill)
        self.maintenance_message = QLabel("Confirm a live plate to check the saved fleet list.")
        self.maintenance_message.setWordWrap(True)
        fleet_card.add(self.maintenance_message)
        self.fleet_table = QTableWidget(0, 4)
        self.fleet_table.setHorizontalHeaderLabels(["License plate", "Truck", "Service task", "Due (YYYY-MM-DD)"])
        self.fleet_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.fleet_table.setMinimumHeight(160)
        fleet_card.add(self.fleet_table)
        actions = QHBoxLayout()
        for title, handler in (("Load demo list", self._demo_fleet), ("Add truck", self._add_truck),
                               ("Remove selected", self._remove_truck), ("Save maintenance list", self._save_fleet)):
            button = QPushButton(title)
            button.clicked.connect(handler)
            actions.addWidget(button)
        fleet_card.add_layout(actions)
        self.fleet_status = QLabel("Save edits before testing live detection.")
        self.fleet_status.setWordWrap(True)
        fleet_card.add(self.fleet_status)
        root.addWidget(fleet_card)

        self.settings_button = QPushButton()
        self.settings_button.setObjectName("iconButton")
        self.settings_button.setFixedSize(34, 34)
        self.settings_button.setToolTip("Show camera and capture settings")
        self.settings_button.setAccessibleName("Camera and capture settings")
        self.settings_button.setCheckable(True)
        camera.actions.addWidget(self.settings_button)
        self.setup = QWidget()
        setup_layout = QHBoxLayout(self.setup)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(12)
        source_card = self._card("Camera & Storage", "Edit while the camera is stopped", "gear")
        rules_card = self._card("Capture Rules", "Confidence, timing and repeat protection", "shield")
        source_form, rules_form = QFormLayout(), QFormLayout()
        for form in (source_form, rules_form):
            form.setVerticalSpacing(12)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.source = QLineEdit(str(self.settings.source))
        self.source.setPlaceholderText("0 or rtsp://camera/stream")
        self.source.setToolTip("USB camera index or RTSP / HTTP stream URL")
        source_form.addRow("Camera source", self.source)
        self.output = QLineEdit(str(self.directory))
        self.output.setToolTip("Folder for images, plate crops and capture history")
        source_form.addRow("Save captures to", self.output)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.0, 1.0)
        self.threshold.setSingleStep(.05)
        self.threshold.setValue(.8)
        self.threshold.setToolTip("Minimum OCR confidence, from 0 to 1")
        rules_form.addRow("Minimum confidence", self.threshold)
        self.confirmations = QSpinBox()
        self.confirmations.setRange(1, 30)
        self.confirmations.setValue(3)
        self.confirmations.setSuffix(" readings")
        rules_form.addRow("Confirm plate", self.confirmations)
        self.cooldown = QSpinBox()
        self.cooldown.setRange(1, 3600)
        self.cooldown.setValue(30)
        self.cooldown.setSuffix(" sec")
        rules_form.addRow("Repeat cooldown", self.cooldown)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(.1, 10)
        self.interval.setValue(.5)
        self.interval.setSuffix(" sec")
        source_form.addRow("Read interval", self.interval)
        source_card.add_layout(source_form)
        rules_card.add_layout(rules_form)
        setup_layout.addWidget(source_card, 1)
        setup_layout.addWidget(rules_card, 1)
        root.addWidget(self.setup)
        self.setup.hide()
        self.settings_button.toggled.connect(self.setup.setVisible)
        self.main_scroll.setWidget(container)
        outer.addWidget(self.main_scroll)
        self.set_theme(self._theme)

    def _card(self, title, subtitle, icon):
        card = Card(title, subtitle, icon=icon, theme=self._theme)
        self.surfaces.append(card)
        return card

    def _detail(self, title, value, card):
        row = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("cardSubtitle")
        text = QLabel(value)
        text.setObjectName("cameraValue")
        text.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(label)
        row.addWidget(text, 1)
        card.add_layout(row)
        return text

    def _controls(self, running):
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.capture_button.setEnabled(running)
        for widget in (self.source, self.threshold, self.confirmations, self.cooldown,
                       self.interval, self.output):
            widget.setEnabled(not running)
        self._sync_hud(running=running)

    def _sync_hud(self, running=None, status=None):
        if running is None:
            running = bool(self.worker and self.worker.isRunning())
        has_frame = self.preview.has_frame() and running
        status = status or ("LIVE" if has_frame else "CONNECTING" if running else "ERROR" if self._error else "STOPPED")
        self.preview_hud.set_plate_state(running, self.auto.isChecked(), status, has_frame,
                                        self._can_control, self._can_capture)

    def _toggle_camera(self):
        if self.worker and self.worker.isRunning():
            if guard(self, "control_engine", "stop license plate recognition"):
                self.stop()
        else:
            self.start()

    def _restart(self):
        if guard(self, "control_engine", "restart license plate recognition"):
            self.stop()
            if self.worker and self.worker.isRunning():
                self._restart_pending = True
                self.status.setText("Restarting after the current reading finishes…")
            else:
                self.start()

    def _open_captures(self):
        if guard(self, "export_data", "open plate captures"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.output.text()).expanduser().resolve())))

    def _open_settings(self):
        self._exit_fullscreen()
        self.settings_button.setChecked(True)
        self.main_scroll.ensureWidgetVisible(self.setup)

    def start(self):
        if not guard(self, "control_engine", "start license plate recognition"):
            return
        if self.worker and self.worker.isRunning():
            return
        value = self.source.text().strip()
        if not value or not self.output.text().strip():
            self.status.setText("Enter a camera source and capture folder.")
            return
        source = int(value) if value.isdecimal() else value
        from face_attendance.dashboard.camera_permission import with_camera_permission
        with_camera_permission(source, self._start_authorized, self._show_error, context=self)

    def _start_authorized(self):
        if not guard(self, "control_engine", "start license plate recognition"):
            return
        if self.worker and self.worker.isRunning():
            return
        value = self.source.text().strip()
        if not value or not self.output.text().strip():
            self.status.setText("Enter a camera source and capture folder.")
            return
        self._load_history()
        self._maintenance_counts.clear()
        source = int(value) if value.isdecimal() else value
        shared = ((lambda: self.engine.camera.frames.get() if self.engine.camera else None)
                  if self.engine.running
                  and str(source) == str(self.engine.config.source) else None)
        config = replace(self.settings.to_config(), source=source)
        worker = PlateWorker(config, Path(self.output.text()).expanduser(), self.threshold.value(),
                             self.confirmations.value(), self.cooldown.value(),
                             self.interval.value(), shared=shared)
        self.worker = worker
        self._stopping = False
        self.plates = []
        self._last_preview_key = None
        self._error = False
        self._reset_reading()
        self.source_label.setText(f"Camera {source}" if isinstance(source, int) else "Network camera")
        self.preview_hud.set_source(self.source_label.text())
        worker.result.connect(self._result)
        worker.saved.connect(self._saved)
        worker.error.connect(self._show_error)
        worker.finished.connect(self._finished)
        self._automatic(self.auto.isChecked())
        self._controls(True)
        self.status.setText("Loading plate models…")
        self.camera_pill.set_status("CONNECTING", "warn")
        self.preview.show_message("Connecting camera", "Preparing license plate recognition…")
        worker.start()
        self.timer.start()

    def stop(self):
        self._restart_pending = False
        self._stopping = True
        self.timer.stop()
        if self.worker:
            self.worker.stop()
        self.preview.show_message("Camera stopped", "Start the camera to read license plates")
        self.camera_pill.set_status("STOPPED", "idle")
        self._reset_reading()
        self.status.setText("Stopped")
        self._controls(bool(self.worker and self.worker.isRunning()))

    def _finished(self):
        if self.sender() is not None and self.sender() is not self.worker:
            return
        self.worker = None
        self.timer.stop()
        self._controls(False)
        self.camera_pill.set_status("ERROR" if self._error else "STOPPED",
                                    "bad" if self._error else "idle")
        self.preview.show_message("Recognition unavailable" if self._error else "Camera stopped",
                                   "Check the message below the camera" if self._error
                                   else "Start the camera to read license plates")
        self._reset_reading()
        if self._restart_pending:
            self._restart_pending = False
            QTimer.singleShot(0, self.start)

    def _show_error(self, message):
        if self.sender() is not None and self.sender() is not self.worker:
            return
        self._error = True
        self.status.setText(message)
        self.camera_pill.set_status("ERROR", "bad")
        self._sync_hud(status="ERROR")

    def _reset_reading(self):
        self.plates = []
        self.plate_text.setText("—")
        self.confidence_label.setText("—")
        self.detected_label.setText("0")
        self.cards["detected"].set_value(0)
        self.plate_pill.set_status("WAITING", "idle")
        self.resolution_label.setText("Resolution —")
        self.latency_label.setText("Analysis —")
        self._last_preview_key = None

    def _automatic(self, enabled):
        if enabled and not guard(self, "export_data", "automatically save plate captures"):
            self.auto.setChecked(False)
            return
        if self.worker:
            (self.worker.automatic.set if enabled else self.worker.automatic.clear)()
        self.mode_label.setText("Auto capture" if enabled else "Manual capture")
        self.cards["mode"].set_value("Auto" if enabled else "Manual")
        self.cards["mode"].set_hint("Save after confirmed readings" if enabled
                                     else "Save with Capture plate")
        self.capture_type_label.setText("Automatic" if enabled else "Manual")
        self._sync_hud()

    def capture(self):
        if guard(self, "export_data", "save a plate capture") and self.worker:
            self.worker.manual.set()
            self.status.setText("Reading the next camera frame…")

    def _result(self, plates):
        if self._stopping or (self.sender() is not None and self.sender() is not self.worker):
            return
        self.plates = plates
        self._reading_at = time.monotonic()
        self._check_maintenance(plates)
        self.cards["detected"].set_value(len(plates))
        self.detected_label.setText(str(len(plates)))
        if plates:
            best = max(plates, key=lambda plate: plate.confidence)
            self.plate_text.setText(best.text)
            self.confidence_label.setText(f"{best.confidence:.0%}")
            self.plate_pill.set_status("DETECTED", "ok")
            self.status.setText("Plate detected. " + (
                "Auto capture waits for matching readings before saving."
                if self.auto.isChecked() else "Select Capture plate to save this reading."))
        else:
            self.plate_text.setText("—")
            self.confidence_label.setText("—")
            self.plate_pill.set_status("SCANNING", "info")
            self.status.setText("Scanning for plates. Keep the plate visible and well lit.")

    def _preview(self):
        # Avoid BGR copies and Qt conversion while another page is visible.
        if not self.isVisible():
            return
        packet = self.worker.preview_packet() if self.worker else None
        frame = packet.frame if packet is not None else None
        if frame is not None:
            key = (getattr(packet, "generation", None), packet.sequence, self._reading_at)
            if key == self._last_preview_key:
                return
            self._last_preview_key = key
            import cv2
            fresh_plates = self.plates if time.monotonic() - self._reading_at <= max(1, self.interval.value()*2) else []
            display = frame.copy() if fresh_plates else frame
            for plate in fresh_plates:
                x1, y1, x2, y2 = plate.box
                cv2.rectangle(display, (x1, y1), (x2, y2), (0, 220, 0), 2)
                cv2.putText(display, f"#{plate.track_id} {plate.text}", (x1, max(20, y1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 220, 0), 2)
            self.preview.set_frame(display)
            height, width = frame.shape[:2]
            self.preview.set_overlay(resolution=f"{width} × {height}")
            self.resolution_label.setText(f"{width} × {height}")
            self.camera_pill.set_status("LIVE", "ok")
            latency = getattr(self.worker, "inference_ms", None)
            if latency is not None:
                self.latency_label.setText(f"Analysis {latency:.0f} ms")
            self._sync_hud(status="LIVE")
            self.preview_hud.metrics.setText(f"{width} × {height}" + (f" · OCR {latency:.0f} ms" if latency is not None else ""))
        elif self.worker and self.worker.isRunning():
            self.preview.show_message("Waiting for camera", "Connecting or restoring the live feed…")
            self.camera_pill.set_status("CONNECTING", "warn")
            self._reset_reading()
            self._sync_hud(status="CONNECTING")

    def _saved(self, record, directory=None, load_image=True):
        self._capture_count += 1
        # Keep an exact session count until the cap; thereafter report a lower
        # bound instead of retaining every OCR string for months of uptime.
        if record["text"] not in self._unique_plates:
            if len(self._unique_plates) < 10_000:
                self._unique_plates.add(record["text"])
            else:
                self._unique_plate_overflow = True
        self.cards["captured"].set_value(self._capture_count)
        unique = f"{len(self._unique_plates):,}+" if self._unique_plate_overflow else len(self._unique_plates)
        self.cards["unique"].set_value(unique)
        if self._unique_plate_overflow:
            self.cards["unique"].set_hint("At least 10,000 different plates this session")
        from datetime import datetime
        captured = datetime.fromisoformat(record["timestamp"])
        self.last_capture_label.setText(captured.strftime("%H:%M:%S UTC"))
        self.last_capture_label.setToolTip(record["timestamp"])
        self.empty_history.hide()
        directory = directory or (self.worker.directory if self.worker else Path(self.output.text()).expanduser())
        import cv2
        crop = cv2.imread(str(directory / record["crop"])) if load_image else None
        pixmap = to_pixmap(crop) if crop is not None else None
        if crop is not None:
            self.crop_preview.setPixmap(pixmap.scaled(
                max(100, self.crop_preview.width() - 16), 74,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.crop_preview.setToolTip(f'Last saved plate: {record["text"]}')
        self.history.insertRow(0)
        for column, text in enumerate((captured.strftime("%H:%M:%S"), record["text"],
                                      f'{record["confidence"]:.0%}', "View" if crop is None else "",
                                      f'#{record["track_id"]}' if record.get("track_id") else "—")):
            item = QTableWidgetItem(text)
            item.setToolTip(f'{record["timestamp"]}\n{directory / record["image"]}')
            item.setData(Qt.ItemDataRole.UserRole, (str(directory), record))
            if column == 3:
                if crop is not None:
                    item.setIcon(QIcon(pixmap.scaled(96, 48,
                        Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
            self.history.setItem(0, column, item)
        self.history.setRowHeight(0, 48)
        if self.history.rowCount() > 200:
            self.history.removeRow(200)

    def _load_history(self):
        self.history.setRowCount(0)
        self._capture_count = 0
        self._unique_plates.clear()
        self._unique_plate_overflow = False
        self.cards["unique"].set_hint("Different plates captured")
        self.empty_history.show()
        try:
            for record in load_captures(Path(self.output.text()).expanduser()):
                # Populate metadata first; decode only the selected image.
                self._saved(record, Path(self.output.text()).expanduser(), load_image=False)
            if self.history.rowCount():
                self.history.selectRow(0)
        except OSError as exc:
            self.status.setText(f"Could not load capture history: {exc}")
        self.cards["captured"].set_hint("Recent saved events (up to 200 loaded)")

    def _select_capture(self):
        selected = self.history.selectedItems()
        item = selected[0] if selected else None
        if item is None:
            return
        directory, record = item.data(Qt.ItemDataRole.UserRole)
        import cv2
        crop = cv2.imread(str(Path(directory) / record["crop"]))
        if crop is None:
            self.crop_preview.setText("Captured image is unavailable")
        else:
            self.crop_preview.setPixmap(to_pixmap(crop).scaled(
                max(100, self.crop_preview.width() - 16), 74,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.crop_preview.setToolTip(f'{record["text"]} • {record["timestamp"]}')

    def _load_fleet(self):
        try:
            self.fleet = load_fleet(self.fleet_path)
            self._render_fleet()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.fleet_status.setText(f"Could not load maintenance list: {exc}")

    def _render_fleet(self):
        self.fleet_table.setRowCount(0)
        for record in self.fleet:
            row = self.fleet_table.rowCount()
            self.fleet_table.insertRow(row)
            for col, key in enumerate(("plate", "truck", "task", "due_date")):
                self.fleet_table.setItem(row, col, QTableWidgetItem(record[key]))

    def _demo_fleet(self):
        if not guard(self, "export_data", "edit maintenance list"):
            return
        if self.fleet_table.rowCount():
            self.fleet_status.setText("Remove existing rows first to load the demo without overwriting your list.")
            return
        self.fleet = demo_fleet()
        self._render_fleet()
        self.fleet_status.setText("Demo loaded. Replace a demo plate with your test truck plate, then save.")

    def _add_truck(self):
        if guard(self, "export_data", "edit maintenance list"):
            self.fleet_table.insertRow(self.fleet_table.rowCount())

    def _remove_truck(self):
        if guard(self, "export_data", "edit maintenance list"):
            self.fleet_table.removeRow(self.fleet_table.currentRow())

    def _save_fleet(self):
        if not guard(self, "export_data", "save maintenance list"):
            return
        records = []
        for row in range(self.fleet_table.rowCount()):
            values = [self.fleet_table.item(row, col).text().strip()
                      if self.fleet_table.item(row, col) else "" for col in range(4)]
            records.append(dict(zip(("plate", "truck", "task", "due_date"), values)))
        try:
            save_fleet(self.fleet_path, records)
        except (OSError, ValueError) as exc:
            self.fleet_status.setText(f"Could not save: {exc}")
            return
        self.fleet = records
        self._maintenance_counts.clear()
        self.fleet_status.setText("Maintenance list saved. Live matching is active, even with auto capture off.")

    def _check_maintenance(self, plates):
        present = {plate_key(plate.text) for plate in plates}
        self._maintenance_counts = {key: self._maintenance_counts.get(key, 0) + 1 for key in present}
        messages, statuses = [], []
        for key in present:
            if self._maintenance_counts[key] < self.confirmations.value():
                continue
            record = next((row for row in self.fleet if plate_key(row["plate"]) == key), None)
            if record is None:
                messages.append(f"{key}: not in the maintenance list")
                continue
            status = maintenance_status(record)
            statuses.append(status)
            messages.append(f'{status} • {record["truck"]} • {record["plate"]} • {record["task"]} • due {record["due_date"]}')
        state = ("OVERDUE" if "OVERDUE" in statuses else "DUE SOON" if "DUE SOON" in statuses
                 else "CLEAR" if statuses else "NOT REGISTERED" if messages else "CHECKING" if plates else "WAITING")
        tone = {"OVERDUE": "bad", "DUE SOON": "warn", "CLEAR": "ok"}.get(state, "idle")
        self.maintenance_pill.set_status(state, tone)
        self.maintenance_message.setText("\n".join(messages) if messages else
            "Checking consecutive plate readings…" if plates else "Waiting for a confirmed live plate.")

    def _engine_changed(self, running):
        worker = self.worker
        if worker and worker.isRunning() and (
                (worker.shared and not running) or
                (not worker.shared and running and
                 str(worker.config.source) == str(self.engine.config.source))):
            self.stop()
            self.status.setText("Attendance camera changed state. Start plate recognition again.")

    def apply_access(self, access):
        self._can_control = access.allows("control_engine")
        self._can_capture = access.allows("export_data")
        if not access.allows("view_dashboard") or not access.allows("control_engine"):
            self._exit_fullscreen()
            self.stop()
        if not access.allows("export_data"):
            self.auto.setChecked(False)
            if self.worker:
                self.worker.manual.clear()
        self.start_button.setVisible(access.allows("control_engine"))
        self.capture_button.setVisible(access.allows("export_data"))
        self.auto.setVisible(access.allows("export_data"))
        self.auto_label.setVisible(access.allows("export_data"))
        self._sync_hud()

    def on_settings_changed(self, settings):
        self.settings = settings

    def set_theme(self, theme):
        self._theme = theme
        for widget in (*self.surfaces, *self.cards.values(), self.preview,
                       self.camera_pill, self.plate_pill, self.maintenance_pill, self.auto):
            widget.set_theme(theme)
        color = palette(theme)["text_secondary"]
        for button, icon in ((self.start_button, "play"), (self.stop_button, "stop"),
                             (self.capture_button, "camera"), (self.settings_button, "gear"),
                             (self.fullscreen_button, "x" if self._fs_active else "expand")):
            apply_button_icon(button, icon, color, 14)

    def _toggle_fullscreen(self):
        if self._fs_active:
            self._exit_fullscreen()
            return
        win = self.window()
        self._fs_active = True
        self._fs_window_state = win.windowState()
        self._fs_scroll = self.main_scroll.verticalScrollBar().value()
        self._fs_margins = self.main_scroll.widget().layout().contentsMargins()
        self._fs_hidden = []
        for widget in (self.details_card, self.fleet_card, self.setup, self.controls_panel,
                       *self.cards.values(), getattr(win, "sidebar", None), getattr(win, "header", None)):
            if widget is not None and not widget.isHidden():
                self._fs_hidden.append(widget)
                widget.hide()
        self.main_scroll.widget().layout().setContentsMargins(0, 0, 0, 0)
        self.preview.setMinimumHeight(0)
        self.preview_hud.set_fullscreen(True)
        self.fullscreen_action.setText("Exit full screen")
        self.fullscreen_button.setText("Exit Full Screen")
        self.escape_shortcut.setEnabled(True)
        win.showFullScreen()
        self.main_scroll.verticalScrollBar().setValue(0)
        self.preview.setFocus()

    def set_detached(self, detached):
        self.preview_hud.set_detached(detached)
        self.detach_action.setText("Return to dashboard" if detached else "Open in new window")

    def _exit_fullscreen(self):
        if not self._fs_active:
            return
        self._fs_active = False
        for widget in self._fs_hidden:
            widget.show()
        self._fs_hidden = []
        self.main_scroll.widget().layout().setContentsMargins(self._fs_margins)
        self.preview.setMinimumHeight(520)
        self.preview_hud.set_fullscreen(False)
        self.fullscreen_action.setText("Enter full screen")
        self.fullscreen_button.setText("Full Screen")
        self.escape_shortcut.setEnabled(False)
        self.window().setWindowState(self._fs_window_state)
        self.main_scroll.verticalScrollBar().setValue(self._fs_scroll)

    def hideEvent(self, event):
        self._exit_fullscreen()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._exit_fullscreen()
        self.stop()
        super().closeEvent(event)
