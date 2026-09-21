"""Settings: camera, recognition, storage and appearance.

``Config`` is frozen and the workers capture it at construction, so saved
values only reach the running engine after a restart. The screen validates
through ``Config`` itself (``Settings.to_config``) exactly like the CLI does.
"""
from pathlib import Path

from PyQt6.QtCore import QThread, QTime, Qt, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QScrollArea, QSpinBox, QTimeEdit, QVBoxLayout,
                             QWidget)

from ...settings import SETTINGS_PATH, Settings, THEMES, describe_source
from ..widgets import ToastBar


class SettingsScreen(QWidget):
    settingsApplied = pyqtSignal(object, bool)  # (settings, restart_engine_requested)

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.fields = {}
        self._build()
        self._connect()
        self.load_settings(settings)

    # --- construction -----------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)
        title = QLabel("Settings")
        title.setObjectName("screenTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("screenSubtitle")
        self.subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(self.subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setSpacing(14)
        column.addWidget(self._build_camera_group())
        column.addWidget(self._build_recognition_group())
        column.addWidget(self._build_storage_group())
        column.addWidget(self._build_report_group())
        column.addWidget(self._build_telegram_group())
        column.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        self.apply_button = QPushButton("Save settings")
        self.apply_restart_button = QPushButton("Save & restart engine")
        self.apply_restart_button.setObjectName("primary")
        self.reset_button = QPushButton("Reset to defaults")
        self.reset_button.setObjectName("danger")
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.apply_restart_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self.toast = ToastBar()
        outer.addWidget(self.toast)

    def _form(self, title):
        group = QGroupBox(title)
        form = QFormLayout(group)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setSpacing(8)
        return group, form

    def _register(self, form, key, label, widget, hint=""):
        form.addRow(label, widget)
        self.fields[key] = widget
        if hint:
            note = QLabel(hint)
            note.setObjectName("screenSubtitle")
            note.setWordWrap(True)
            form.addRow("", note)
        return widget

    def _build_camera_group(self):
        group, form = self._form("Camera")
        self.source_type = QComboBox()
        self.source_type.addItem("Webcam / device index", "device")
        self.source_type.addItem("Network URL (RTSP/HTTP)", "url")
        self.source_type.addItem("Video file", "file")
        form.addRow("Source type", self.source_type)
        self.fields["source_type"] = self.source_type

        self.source_value = QLineEdit()
        self.source_value.setPlaceholderText("0, 1, rtsp://user:pass@host:554/stream1, or a video file")
        row = QHBoxLayout()
        row.addWidget(self.source_value, 1)
        self.browse_source_button = QPushButton("Choose file")
        row.addWidget(self.browse_source_button)
        container = QWidget()
        container.setLayout(row)
        self._register(form, "source", "Source", container,
                       "RTSP credentials are stored in settings.json (gitignored) and are never "
                       "written to the log. Prefer a camera user without a password where possible.")

        for key, label, low, high, step, decimals, hint in (
                ("camera_width", "Frame width", 160, 4096, 10, 0, ""),
                ("camera_height", "Frame height", 120, 2160, 10, 0, ""),
                ("target_fps", "Target FPS", 1, 120, 1, 1, "Requested from the driver, not guaranteed."),
                ("detection_scale", "Detector scale", 0.1, 1.0, 0.05, 2,
                 "Smaller is faster; 0.25 is the terminal default."),
                ("detection_interval", "Detection interval", 1, 30, 1, 0, "Camera frames between detections."),
                ("recognition_interval", "Recognition interval", 1, 60, 1, 0,
                 "Frames between encodings for unconfirmed faces."),
                ("camera_timeout_ms", "Open/read timeout", 250, 30000, 250, 0,
                 "Applies to FFmpeg network sources."),
                ("reconnect_sec", "Reconnect delay", 0.2, 30, 0.1, 1, "Backoff after a disconnect.")):
            box = QDoubleSpinBox() if decimals else QSpinBox()
            box.setRange(low, high)
            box.setSingleStep(step)
            if decimals:
                box.setDecimals(decimals)
            self._register(form, key, label, box, hint)
        return group

    def _build_recognition_group(self):
        group, form = self._form("Recognition and attendance")
        for key, label, low, high, step, decimals, hint in (
                ("max_detect_faces", "Max faces to detect", 1, 20, 1, 0,
                 "Upper limit on simultaneous faces the engine will process per frame. "
                 "Higher values use more CPU; 5 is a sensible default for most setups."),
                ("face_tolerance", "Face tolerance", 0.3, 0.9, 0.01, 2,
                 "Lower is stricter. Loosening this never fixes duplicate enrollment data."),
                ("identity_margin", "Identity margin", 0.0, 0.3, 0.005, 3,
                 "Minimum separation from a competing employee."),
                ("min_confirmation_frames", "Confirmation frames", 1, 20, 1, 0,
                 "Matching encodings required before an identity is trusted."),
                ("stable_recheck_sec", "Stable recheck", 0.1, 5, 0.05, 2,
                 "How often a stable track is re-encoded near capture."),
                ("capture_after_sec", "Verified presence", 0.5, 30, 0.5, 1,
                 "Seconds of continuous verified presence before a check-in."),
                ("cooldown_sec", "Employee cooldown", 0, 3600, 5, 0,
                 "Repeat check-ins for one employee are blocked for this long."),
                ("opencv_threads", "OpenCV threads", 1, 16, 1, 0,
                 "Keep at 1 unless you have measured spare CPU.")):
            box = QDoubleSpinBox() if decimals else QSpinBox()
            box.setRange(low, high)
            box.setSingleStep(step)
            if decimals:
                box.setDecimals(decimals)
            self._register(form, key, label, box, hint)
        return group

    def _build_storage_group(self):
        group, form = self._form("Storage")
        for key, label, caption in (
                ("db_path", "Attendance database", "SQLite authority for attendance"),
                ("capture_dir", "Evidence images", "Clean JPEG snapshots per check-in"),
                ("encodings_path", "Face encodings", "encodings.pickle used by the engine"),
                ("employees_path", "Employee map", "employees.json label to HRM ID"),
                ("log_path", "Observation log", "Throttled CSV of recognition observations"),
                ("alert_path", "Alert sound", "Optional WAV played on a check-in")):
            edit = QLineEdit()
            button = QPushButton("Browse")
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            row.addWidget(button)
            container = QWidget()
            container.setLayout(row)
            button.clicked.connect(lambda _=False, e=edit, k=key: self._browse_path(e, k))
            self._register(form, key, label, container, caption)
            self.fields[key] = edit
        self._register(form, "persistence_queue_size", "Evidence queue size",
                       self._spin(1, 64))
        return group

    def _build_report_group(self):
        group, form = self._form("Appearance and reporting")
        theme = QComboBox()
        for name in THEMES:
            theme.addItem(name.capitalize(), name)
        self._register(form, "theme", "Theme", theme)
        self._register(form, "report_page_size", "Report page size", self._spin(10, 1000))
        work = QTimeEdit(QTime(9, 0))
        work.setDisplayFormat("HH:mm")
        self.work_start_check = QCheckBox("Add a Punctuality column using this start time")
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(work, 1)
        layout.addWidget(self.work_start_check, 2)
        self._register(form, "report_work_start", "Work start", holder,
                       "Off by default: the database has no late/absent policy, so this label is "
                       "computed at export time only.")
        self.fields["report_work_start"] = work
        return group

    def _build_telegram_group(self):
        group, form = self._form("Telegram Notifications")
        token_edit = QLineEdit()
        token_edit.setPlaceholderText("123456789:ABCdefGHIjklMNOpqrSTUvwxYZ")
        token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._register(form, "telegram_bot_token", "Bot token", token_edit,
                       "Create a bot with @BotFather on Telegram and paste the token here.")
        chat_edit = QLineEdit()
        chat_edit.setPlaceholderText("-1001234567890 or your user ID")
        self._register(form, "telegram_chat_id", "Chat ID", chat_edit,
                       "Send /start to your bot, then use @userinfobot or @RawDataBot to find your chat ID. "
                       "For groups, add the bot and use the group's chat ID (starts with -).")
        self.tg_capture_check = QCheckBox("Send photo and details on every attendance capture")
        self.tg_capture_check.setChecked(True)
        form.addRow("On capture", self.tg_capture_check)
        self.fields["telegram_notify_capture"] = self.tg_capture_check
        self.tg_unknown_check = QCheckBox("Send danger alert when an unknown face is detected")
        self.tg_unknown_check.setChecked(True)
        form.addRow("On unknown face", self.tg_unknown_check)
        self.fields["telegram_notify_unknown"] = self.tg_unknown_check
        self.tg_test_button = QPushButton("Verify Bot & Send Test Message")
        self.tg_test_button.setObjectName("primary")
        self.tg_test_result = QLabel("")
        self.tg_test_result.setObjectName("screenSubtitle")
        self.tg_test_result.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.tg_test_button)
        row.addWidget(self.tg_test_result, 1)
        container = QWidget()
        container.setLayout(row)
        form.addRow("", container)
        self.tg_test_button.clicked.connect(self._test_telegram)
        return group

    def _test_telegram(self):
        token = self.fields["telegram_bot_token"].text().strip()
        chat_id = self.fields["telegram_chat_id"].text().strip()
        if not token or not chat_id:
            self.tg_test_result.setText("Enter both a bot token and chat ID first.")
            return
        self.tg_test_button.setEnabled(False)
        self.tg_test_result.setText("Connecting...")
        self._tg_worker = _TelegramTestWorker(token, chat_id)
        self._tg_worker.finished.connect(self._on_telegram_test_done)
        self._tg_worker.start()

    def _on_telegram_test_done(self, result):
        self.tg_test_result.setText(result)
        self.tg_test_button.setEnabled(True)
        tone = "ok" if "successfully" in result.lower() else "bad"
        self.toast.show_message(result, tone)

    @staticmethod
    def _spin(low, high):
        box = QSpinBox()
        box.setRange(low, high)
        return box

    def _browse_path(self, edit, key):
        if key == "capture_dir":
            chosen = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text() or str(Path.home()))
        else:
            chosen, _ = QFileDialog.getSaveFileName(self, "Choose file", edit.text() or str(Path.home()))
        if chosen:
            edit.setText(chosen)

    # --- wiring -----------------------------------------------------------
    def _connect(self):
        self.apply_button.clicked.connect(lambda: self.apply(restart=False))
        self.apply_restart_button.clicked.connect(lambda: self.apply(restart=True))
        self.reset_button.clicked.connect(self._reset)
        self.browse_source_button.clicked.connect(self._browse_source)
        self.source_type.currentIndexChanged.connect(self._source_type_changed)

    def _source_type_changed(self):
        self.browse_source_button.setEnabled(self.source_type.currentData() == "file")

    def _browse_source(self):
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose a video file",
                                                self.source_value.text() or str(Path.home()),
                                                "Video files (*.mp4 *.mov *.avi *.mkv *.m4v);;All files (*)")
        if chosen:
            self.source_value.setText(chosen)
            self._select_data(self.source_type, "file")

    @staticmethod
    def _select_data(combo, value):
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def load_settings(self, settings):
        self.settings = settings
        source = str(settings.source).strip()
        self._select_data(self.source_type, "device" if source.isdecimal()
                          else "url" if "://" in source else "file")
        self._source_type_changed()
        self.source_value.setText(source)
        for key, widget in self.fields.items():
            if key in ("source_type", "source"):
                continue
            value = getattr(settings, key, None)
            if key == "report_work_start":
                self.work_start_check.setChecked(bool(value))
                if value:
                    parts = (str(value).split(":") + ["0", "0"])[:2]
                    widget.setTime(QTime(int(parts[0] or 0), int(parts[1] or 0)))
                continue
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QComboBox):
                self._select_data(widget, value)
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value) if value else "")
            else:
                if isinstance(widget, QDoubleSpinBox):
                    widget.setValue(float(value) if value is not None else 0.0)
                else:
                    widget.setValue(int(value) if value is not None else 0)
        self.subtitle.setText(f"Stored in {SETTINGS_PATH.name}. "
                              f"Active source: {describe_source(settings.source)}")

    # --- save / reset -----------------------------------------------------
    def collect(self):
        """Raw form values; validated afterwards by ``Settings.to_config``."""
        source = self.source_value.text().strip()
        kind = self.source_type.currentData()
        if not source:
            raise ValueError("Enter a camera index, network URL or file path")
        if kind == "device" and not source.isdecimal():
            raise ValueError("A webcam source must be a device index such as 0 or 1")
        if kind == "file" and not Path(source).exists():
            raise ValueError(f"Video file not found: {source}")
        values = {"source": source}
        for key, widget in self.fields.items():
            if key in ("source_type", "source"):
                continue
            if key == "report_work_start":
                values[key] = widget.time().toString("HH:mm") if self.work_start_check.isChecked() else ""
            elif isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, QComboBox):
                values[key] = widget.currentData()
            elif isinstance(widget, QLineEdit):
                values[key] = widget.text().strip()
            else:
                values[key] = widget.value()
        return values

    def apply(self, restart=False):
        try:
            values = self.collect()
            self.settings.update(values)
            self.settings.to_config()  # Same validation path as the command line.
        except ValueError as exc:
            self.toast.show_message(str(exc), "bad")
            return False
        try:
            path = self.settings.save()
        except OSError as exc:
            self.toast.show_message(f"Could not write settings: {exc}", "bad")
            return False
        note = f"Saved {path.name}."
        if restart:
            try:
                self.engine.restart(self.settings)
                note += " Engine restarted; the source and tuning are live now."
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                self.toast.show_message(f"Settings saved, but the engine restart failed: {exc}", "bad")
                self.settingsApplied.emit(self.settings, False)
                return True
        else:
            note += " Restart the engine (or use Save & restart) to apply camera or recognition changes."
        self.load_settings(self.settings)
        self.toast.show_message(note, "ok")
        self.settingsApplied.emit(self.settings, restart)
        return True

    def _reset(self):
        confirmed = QMessageBox.question(
            self, "Reset settings",
            "Restore every dashboard setting to its default and delete settings.json?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        self.load_settings(Settings.reset())
        self.settingsApplied.emit(self.settings, False)
        self.toast.show_message("Defaults restored. Restart the engine to apply them.", "ok")

    def on_settings_changed(self, settings):
        self.load_settings(settings)

    def set_theme(self, theme):
        return None  # Colours come from the application stylesheet.


class _TelegramTestWorker(QThread):
    finished = pyqtSignal(str)

    def __init__(self, token, chat_id):
        super().__init__()
        self._token = token
        self._chat_id = chat_id

    def run(self):
        from ..telegram import TelegramService
        service = TelegramService(self._token, self._chat_id)
        result = service.send_test()
        service.stop()
        self.finished.emit(result)