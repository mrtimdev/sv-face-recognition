"""Live license plate screen; camera reads, inference and disk writes stay off Qt."""
from dataclasses import replace
from pathlib import Path
import threading
import time

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QDoubleSpinBox, QSpinBox, QFrame,
                             QScrollArea, QSizePolicy, QHeaderView,
                             QFormLayout, QTableWidget, QTableWidgetItem)

from face_attendance.camera import CameraManager
from face_attendance.config import ROOT
from face_attendance.dashboard.access import guard
from face_attendance.dashboard.bridge import to_pixmap
from face_attendance.dashboard.threads import settle
from face_attendance.dashboard.icons import apply_button_icon
from face_attendance.dashboard.theme import palette
from face_attendance.dashboard.widgets import Card, StatCard, StatusPill, ToggleSwitch, VideoView
from .core import CapturePolicy, create_recognizer, read_plates, save_capture


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
        self.shared, self.recognizer_factory = shared, recognizer_factory
        self.camera = None if shared else CameraManager(config, capture_factory)
        self.automatic = threading.Event()
        self.manual = threading.Event()
        self.latest = None

    def stop(self):
        self.requestInterruption()
        if self.camera:
            self.camera.stop_event.set()
        settle(self)

    def run(self):
        try:
            recognizer = self.recognizer_factory()
            if self.isInterruptionRequested():
                return
            if self.camera:
                self.camera.start()
            next_read, last_sequence = 0, -1
            while not self.isInterruptionRequested():
                now = time.monotonic()
                packet = self.shared() if self.shared else self.camera.frames.get()
                if packet is None or now - packet.captured_at > self.config.camera_stale_sec:
                    self.latest = None
                    self.policy.counts.clear()
                    self.msleep(20)
                    continue
                if packet.sequence == last_sequence:
                    self.msleep(20)
                    continue
                last_sequence = packet.sequence
                frame = packet.frame
                self.latest = frame
                if now < next_read:
                    self.msleep(20)
                    continue
                next_read = now + self.interval
                plates = read_plates(recognizer, frame, self.threshold)
                if self.isInterruptionRequested():
                    break
                eligible = self.policy.eligible(plates, now)
                manual = self.manual.is_set()
                self.manual.clear()
                candidates = plates if manual else eligible if self.automatic.is_set() else []
                for plate in {p.text: p for p in candidates}.values():
                    record = save_capture(self.directory, frame, plate, self.config.source, not manual)
                    self.policy.mark_saved(plate, now)
                    self.saved.emit(record)
                self.result.emit(plates)
        except Exception as exc:
            self.error.emit(str(exc))
        finally:
            self.latest = None
            if self.camera:
                self.camera.close()


class LicensePlateScreen(QWidget):
    def __init__(self, engine, settings):
        super().__init__()
        self.engine, self.settings = engine, settings
        self.worker = None
        self.plates = []
        self._theme = settings.theme
        self._capture_count = 0
        self._unique_plates = set()
        self._error = False
        self.directory = ROOT / "plate_captures"
        self._build()
        self.start_button.clicked.connect(self.start)
        self.stop_button.clicked.connect(self.stop)
        self.capture_button.clicked.connect(self.capture)
        self.auto.toggled.connect(self._automatic)
        self.timer = QTimer(self)
        self.timer.setInterval(80)
        self.timer.timeout.connect(self._preview)
        engine.runningChanged.connect(self._engine_changed)
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
        self.camera_pill = StatusPill("STOPPED", "idle", dot=True, theme=self._theme)
        camera.actions.addWidget(self.camera_pill)
        self.preview = VideoView(theme=self._theme, message="Ready to read license plates",
                                 subtitle="Start the camera to begin recognition")
        self.preview.set_guide(False)
        self.preview.setMinimumHeight(400)
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
        info.addWidget(self.source_label, 1)
        info.addWidget(self.resolution_label)
        info.addWidget(self.mode_label)
        camera.add_layout(info)

        controls = QHBoxLayout()
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
        camera.add_layout(controls)
        self.status = QLabel("Start the camera. Capture manually or turn on auto capture.")
        self.status.setObjectName("cardSubtitle")
        self.status.setWordWrap(True)
        camera.add(self.status)
        body.addWidget(camera, 7)

        details = self._card("Plate Details", "Latest recognition", "shield")
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
        self.history = QTableWidget(0, 4)
        self.history.setHorizontalHeaderLabels(["Time (UTC)", "Plate", "Score", "Image"])
        self.history.setColumnHidden(3, True)
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
        shared = ((lambda: self.engine.camera.frames.get() if self.engine.camera else None)
                  if self.engine.running
                  and str(source) == str(self.engine.config.source) else None)
        config = replace(self.settings.to_config(), source=source)
        worker = PlateWorker(config, Path(self.output.text()).expanduser(), self.threshold.value(),
                             self.confirmations.value(), self.cooldown.value(),
                             self.interval.value(), shared=shared)
        self.worker = worker
        self.plates = []
        self._error = False
        self._reset_reading()
        self.source_label.setText(f"Camera {source}" if isinstance(source, int) else "Network camera")
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
        self.timer.stop()
        if self.worker:
            self.worker.stop()
        self.preview.show_message("Camera stopped", "Start the camera to read license plates")
        self.camera_pill.set_status("STOPPED", "idle")
        self._reset_reading()
        self.status.setText("Stopped")
        self._controls(bool(self.worker and self.worker.isRunning()))

    def _finished(self):
        self.timer.stop()
        self._controls(False)
        self.camera_pill.set_status("ERROR" if self._error else "STOPPED",
                                    "bad" if self._error else "idle")
        self.preview.show_message("Recognition unavailable" if self._error else "Camera stopped",
                                   "Check the message below the camera" if self._error
                                   else "Start the camera to read license plates")
        self._reset_reading()

    def _show_error(self, message):
        self._error = True
        self.status.setText(message)
        self.camera_pill.set_status("ERROR", "bad")

    def _reset_reading(self):
        self.plates = []
        self.plate_text.setText("—")
        self.confidence_label.setText("—")
        self.detected_label.setText("0")
        self.cards["detected"].set_value(0)
        self.plate_pill.set_status("WAITING", "idle")
        self.resolution_label.setText("Resolution —")

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

    def capture(self):
        if guard(self, "export_data", "save a plate capture") and self.worker:
            self.worker.manual.set()
            self.status.setText("Reading the next camera frame…")

    def _result(self, plates):
        self.plates = plates
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
        frame = self.worker.latest if self.worker else None
        if frame is not None:
            import cv2
            display = frame.copy()
            for plate in self.plates:
                x1, y1, x2, y2 = plate.box
                cv2.rectangle(display, (x1, y1), (x2, y2), (0, 220, 0), 2)
                cv2.putText(display, plate.text, (x1, max(20, y1 - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 220, 0), 2)
            self.preview.set_frame(display)
            height, width = frame.shape[:2]
            self.preview.set_overlay(resolution=f"{width} × {height}")
            self.resolution_label.setText(f"{width} × {height}")
            self.camera_pill.set_status("LIVE", "ok")
        elif self.worker and self.worker.isRunning():
            self.preview.show_message("Waiting for camera", "Connecting or restoring the live feed…")
            self.camera_pill.set_status("CONNECTING", "warn")
            self._reset_reading()

    def _saved(self, record):
        self._capture_count += 1
        self._unique_plates.add(record["text"])
        self.cards["captured"].set_value(self._capture_count)
        self.cards["unique"].set_value(len(self._unique_plates))
        from datetime import datetime
        captured = datetime.fromisoformat(record["timestamp"])
        self.last_capture_label.setText(captured.strftime("%H:%M:%S UTC"))
        self.last_capture_label.setToolTip(record["timestamp"])
        self.empty_history.hide()
        directory = self.worker.directory if self.worker else Path(self.output.text()).expanduser()
        import cv2
        crop = cv2.imread(str(directory / record["crop"]))
        if crop is not None:
            self.crop_preview.setPixmap(to_pixmap(crop).scaled(
                max(100, self.crop_preview.width() - 16), 74,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.crop_preview.setToolTip(f'Last saved plate: {record["text"]}')
        self.history.insertRow(0)
        for column, text in enumerate((captured.strftime("%H:%M:%S"), record["text"],
                                      f'{record["confidence"]:.0%}', record["image"])):
            item = QTableWidgetItem(text)
            item.setToolTip(f'{record["timestamp"]}\n{directory / record["image"]}')
            self.history.setItem(0, column, item)
        if self.history.rowCount() > 200:
            self.history.removeRow(200)

    def _engine_changed(self, running):
        worker = self.worker
        if worker and worker.isRunning() and (
                (worker.shared and not running) or
                (not worker.shared and running and
                 str(worker.config.source) == str(self.engine.config.source))):
            self.stop()
            self.status.setText("Attendance camera changed state. Start plate recognition again.")

    def apply_access(self, access):
        if not access.allows("view_dashboard") or not access.allows("control_engine"):
            self.stop()
        if not access.allows("export_data"):
            self.auto.setChecked(False)
            if self.worker:
                self.worker.manual.clear()
        self.start_button.setVisible(access.allows("control_engine"))
        self.capture_button.setVisible(access.allows("export_data"))
        self.auto.setVisible(access.allows("export_data"))
        self.auto_label.setVisible(access.allows("export_data"))

    def on_settings_changed(self, settings):
        self.settings = settings

    def set_theme(self, theme):
        self._theme = theme
        for widget in (*self.surfaces, *self.cards.values(), self.preview,
                       self.camera_pill, self.plate_pill, self.auto):
            widget.set_theme(theme)
        color = palette(theme)["text_secondary"]
        for button, icon in ((self.start_button, "play"), (self.stop_button, "stop"),
                             (self.capture_button, "camera"), (self.settings_button, "gear")):
            apply_button_icon(button, icon, color, 14)

    def closeEvent(self, event):
        self.stop()
        super().closeEvent(event)
