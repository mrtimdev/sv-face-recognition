"""Settings: camera, recognition, storage and appearance.

``Config`` is frozen and the workers capture it at construction, so saved
values only reach the running engine after a restart. The screen validates
through ``Config`` itself (``Settings.to_config``) exactly like the CLI does.
"""
from pathlib import Path

from PyQt6.QtCore import QThread, QTime, Qt, pyqtSignal
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDoubleSpinBox,
                             QFileDialog, QFormLayout, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QScrollArea, QSpinBox, QStackedWidget,
                             QTimeEdit, QVBoxLayout, QWidget)

from ..access import guard
from ..threads import settle
from ...settings import SETTINGS_PATH, Settings, THEMES, describe_source, hash_pin
from ..icons import apply_button_icon
from ..theme import palette
from ..widgets import Card, PageHeader, StatCard, ToastBar
from .updates import UpdatesPage


class SettingsScreen(QWidget):
    settingsApplied = pyqtSignal(object, bool)  # (settings, restart_engine_requested)

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.fields = {}
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self._cards = []
        self._build()
        self._connect()
        self.load_settings(settings)

    # --- construction -----------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(16)

        self.header = PageHeader("", "Configure camera, recognition, storage and appearance.")
        self.header.layout().setStretch(0, 1)
        self.apply_button = QPushButton("Save")
        self.apply_restart_button = QPushButton("Save && restart")
        self.apply_restart_button.setObjectName("primary")
        self.reset_button = QPushButton("Reset defaults")
        self.reset_button.setObjectName("danger")
        self.header.add_action(self.apply_button)
        self.header.add_action(self.apply_restart_button)
        self.header.add_action(self.reset_button)
        outer.addWidget(self.header)

        self.subtitle = QLabel("")
        self.subtitle.setObjectName("screenSubtitle")
        self.subtitle.setWordWrap(True)
        outer.addWidget(self.subtitle)

        content = QHBoxLayout()
        content.setSpacing(14)
        self._tab_strip = self._build_tab_strip()
        content.addWidget(self._tab_strip)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._make_page(
            self._build_camera_group(), self._build_sound_group()))
        self._stack.addWidget(self._make_page(self._build_recognition_group()))
        self._stack.addWidget(self._make_page(
            self._build_storage_group(), self._build_database_group()))
        self._stack.addWidget(self._make_page(self._build_report_group()))
        self._stack.addWidget(self._make_page(self._build_telegram_group()))
        self._stack.addWidget(self._make_page(self._build_security_group()))
        self._stack.addWidget(self._build_backup_page())
        self.updates_page = UpdatesPage(self.settings, self._theme)
        self._stack.addWidget(self.updates_page)
        content.addWidget(self._stack, 1)
        outer.addLayout(content, 1)

        self.toast = ToastBar(theme=self._theme)
        outer.addWidget(self.toast)

        self._apply_icons()
        self._select_tab(0)

    def _build_tab_strip(self):
        strip = QFrame()
        strip.setObjectName("settingsStrip")
        strip.setFixedWidth(196)
        layout = QVBoxLayout(strip)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(2)

        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        self._tab_buttons = []

        tabs = [
            ("camera", "Camera && Capture"),
            ("face-id", "Recognition"),
            ("database", "Data && Storage"),
            ("sliders", "Appearance"),
            ("send", "Notifications"),
            ("shield", "Security"),
            ("download", "Backup"),
            ("refresh", "Updates"),
        ]
        for i, (icon_name, label) in enumerate(tabs):
            btn = QPushButton(f"  {label}")
            btn.setObjectName("settingsTab")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._tab_group.addButton(btn, i)
            self._tab_buttons.append((btn, icon_name))
            layout.addWidget(btn)

        layout.addStretch(1)
        self._tab_buttons[0][0].setChecked(True)
        self._tab_group.idClicked.connect(self._select_tab)
        return strip

    def _make_page(self, *cards):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        for card in cards:
            column.addWidget(card)
        column.addStretch(1)
        scroll.setWidget(holder)
        return scroll

    def _select_tab(self, index):
        self._stack.setCurrentIndex(index)
        self._update_tab_icons()
        if index == 6:
            self._refresh_backup_info()

    def _update_tab_icons(self):
        colors = palette(self._theme)
        checked_id = self._tab_group.checkedId()
        for i, (btn, icon_name) in enumerate(self._tab_buttons):
            color = colors["primary_soft_fg"] if i == checked_id else colors["muted"]
            apply_button_icon(btn, icon_name, color)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _card_form(self, title, subtitle="", icon=""):
        card = Card(title, subtitle, icon=icon, theme=self._theme)
        self._cards.append(card)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setSpacing(8)
        card.add_layout(form)
        return card, form

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
        card, form = self._card_form("Camera", "Video source and frame capture settings.", icon="camera")
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
                 "0.50 is the default; 0.25 uses less CPU. Compare Analysis latency in Live Monitor."),
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
        return card

    def _build_recognition_group(self):
        card, form = self._card_form("Recognition and attendance",
                                     "Face matching thresholds and check-in timing.", icon="face-id")
        for key, label, low, high, step, decimals, hint in (
                ("max_detect_faces", "Max faces to detect", 1, 20, 1, 0,
                 "Upper limit on simultaneous faces the engine will process per frame. "
                 "Higher values use more CPU; 5 is a sensible default for most setups."),
                ("quality_min_face_px", "Minimum face pixels", 80, 300, 10, 0,
                 "Shortest face dimension in the original frame. Smaller faces receive Move closer guidance."),
                ("quality_min_sharpness", "Minimum sharpness", 1, 200, 1, 1,
                 "Blur check on a 96 × 96 face image. Calibrate for your camera; this is not a liveness score."),
                ("face_tolerance", "SFace cosine distance", 0.3, 0.9, 0.01, 2,
                 "Lower is stricter. Loosening this never fixes duplicate enrollment data."),
                ("identity_margin", "Identity margin", 0.0, 0.3, 0.005, 3,
                 "Minimum cosine-distance separation from a competing employee."),
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
        return card

    def _build_sound_group(self):
        card, form = self._card_form("Built-in sounds", "Distinct local tones; no internet required.", icon="sliders")
        for key, label in (("sounds_enabled", "Enable sounds"),
                           ("sound_detection", "Face detected / no face in view"),
                           ("sound_guidance", "Position and lighting guidance"),
                           ("sound_unknown", "Not enrolled")):
            self._register(form, key, label, QCheckBox())
        note = QLabel("Verification, saved attendance, errors and camera disconnection have separate tones. "
                      "Alerts are debounced and never overlap. Preview each tone in Live Monitor.")
        note.setWordWrap(True)
        form.addRow(note)
        return card

    def _build_storage_group(self):
        card, form = self._card_form("Storage", "File paths for data, evidence and logs.", icon="database")
        for key, label, caption in (
                ("db_path", "Attendance database", "SQLite authority for attendance"),
                ("capture_dir", "Evidence images", "Clean JPEG snapshots per check-in"),
                ("encodings_path", "Face encodings", "Versioned SFace templates used by the engine"),
                ("employees_path", "Employee map", "employees.json label to HRM ID"),
                ("log_path", "Observation log", "Throttled CSV of recognition observations"),
                ("alert_path", "Custom success sound", "Optional WAV override; the built-in success tone is used when unavailable")):
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
        return card

    def _build_database_group(self):
        card, form = self._card_form("Remote Database",
                                     "Connect to a remote PostgreSQL or MySQL server. "
                                     "Leave on SQLite for local-only operation.", icon="database")
        backend = QComboBox()
        backend.addItem("SQLite (local file)", "sqlite")
        backend.addItem("PostgreSQL (remote)", "postgresql")
        backend.addItem("MySQL (remote)", "mysql")
        self._register(form, "db_backend", "Backend", backend)

        self._db_remote_widgets = []

        host = QLineEdit()
        host.setPlaceholderText("e.g. 192.168.1.100 or db.example.com")
        self._register(form, "db_host", "Host", host)
        self._db_remote_widgets.append(("db_host", host))

        port = QSpinBox()
        port.setRange(1, 65535)
        port.setValue(3306)
        self._register(form, "db_port", "Port", port)
        self._db_remote_widgets.append(("db_port", port))

        user = QLineEdit()
        user.setPlaceholderText("database username")
        self._register(form, "db_user", "User", user)
        self._db_remote_widgets.append(("db_user", user))

        password = QLineEdit()
        password.setEchoMode(QLineEdit.EchoMode.Password)
        password.setPlaceholderText("database password")
        self._register(form, "db_password", "Password", password)
        self._db_remote_widgets.append(("db_password", password))

        db_name = QLineEdit()
        db_name.setPlaceholderText("sv_attendance")
        self._register(form, "db_name", "Database name", db_name)
        self._db_remote_widgets.append(("db_name", db_name))

        self.db_test_button = QPushButton("Test Connection")
        self.db_test_button.setObjectName("softButton")
        self.db_test_result = QLabel("")
        self.db_test_result.setObjectName("screenSubtitle")
        self.db_test_result.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.db_test_button)
        row.addWidget(self.db_test_result, 1)
        container = QWidget()
        container.setLayout(row)
        form.addRow("", container)
        self._db_remote_widgets.append(("_test", container))

        backend.currentIndexChanged.connect(self._db_backend_changed)
        self.db_test_button.clicked.connect(self._test_db_connection)
        self._db_backend_changed()
        return card

    def _db_backend_changed(self):
        is_remote = self.fields["db_backend"].currentData() != "sqlite"
        for _key, widget in self._db_remote_widgets:
            widget.setVisible(is_remote)
            label = self._find_form_label(widget)
            if label:
                label.setVisible(is_remote)
        if is_remote:
            backend = self.fields["db_backend"].currentData()
            port_widget = self.fields["db_port"]
            if backend == "postgresql" and port_widget.value() in (0, 3306):
                port_widget.setValue(5432)
            elif backend == "mysql" and port_widget.value() in (0, 5432):
                port_widget.setValue(3306)

    @staticmethod
    def _find_form_label(widget):
        parent = widget.parentWidget()
        if parent is None:
            return None
        for child_layout in parent.findChildren(QFormLayout):
            for i in range(child_layout.rowCount()):
                field_item = child_layout.itemAt(i, QFormLayout.ItemRole.FieldRole)
                if field_item and field_item.widget() is widget:
                    label_item = child_layout.itemAt(i, QFormLayout.ItemRole.LabelRole)
                    return label_item.widget() if label_item else None
        return None

    def _test_db_connection(self):
        backend = self.fields["db_backend"].currentData()
        if backend == "sqlite":
            self.db_test_result.setText("SQLite is local — no remote connection to test.")
            return
        self.db_test_button.setEnabled(False)
        self.db_test_result.setText("Connecting...")

        class _Cfg:
            pass
        cfg = _Cfg()
        cfg.db_backend = backend
        cfg.db_host = self.fields["db_host"].text().strip()
        cfg.db_port = self.fields["db_port"].value()
        cfg.db_user = self.fields["db_user"].text().strip()
        cfg.db_password = self.fields["db_password"].text()
        cfg.db_name = self.fields["db_name"].text().strip() or "sv_attendance"
        cfg.db_sslmode = ""

        self._db_test_worker = _DbTestWorker(cfg)
        self._db_test_worker.finished.connect(self._on_db_test_done)
        self._db_test_worker.start()

    def _on_db_test_done(self, result):
        self.db_test_result.setText(result)
        self.db_test_button.setEnabled(True)
        tone = "ok" if "success" in result.lower() else "bad"
        self.toast.show_message(result, tone)

    # --- backup ---------------------------------------------------------------

    def _refresh_backup_info(self):
        for sc in self._backup_stats:
            sc.set_value("…")
            sc.set_hint("scanning…")
        self._backup_scan_worker = _BackupScanWorker(self.settings)
        self._backup_scan_worker.finished.connect(self._on_backup_scan_done)
        self._backup_scan_worker.start()

    def _on_backup_scan_done(self, info):
        self._bk_backend.set_value(info["backend_label"], "ok")
        self._bk_backend.set_hint(info["backend_hint"])
        tc = info["table_count"]
        self._bk_tables.set_value(str(tc), "ok" if tc else "idle")
        self._bk_tables.set_hint(f"{tc} of 4 found" if tc else "none found")
        tr = info["total_rows"]
        self._bk_records.set_value(f"{tr:,}", "ok" if tr else "idle")
        self._bk_records.set_hint("total across tables")
        self._bk_size.set_value(info["size_str"], "idle")
        self._bk_size.set_hint(info["size_hint"])

    UPDATES_TAB = 7

    def show_updates(self):
        """Open the Updates tab (used by the startup prompt and notifications)."""
        self._tab_group.button(self.UPDATES_TAB).setChecked(True)
        self._select_tab(self.UPDATES_TAB)

    def closeEvent(self, event):
        # A connection test or backup still running must not be destroyed mid-run.
        for name in ("_db_test_worker", "_backup_scan_worker", "_backup_worker", "_tg_worker"):
            settle(getattr(self, name, None), 0)
        self.updates_page.close()
        super().closeEvent(event)

    def _export_backup(self):
        if not guard(self, "export_data", "export database backups"):
            return
        from datetime import datetime as _dt
        default_name = f"sv_attendance_backup_{_dt.now().strftime('%Y%m%d_%H%M%S')}.sql"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export database backup",
            str(Path.home() / default_name),
            "SQL files (*.sql);;All files (*)")
        if not path:
            return
        self._backup_button.setEnabled(False)
        self._backup_status.setText("Exporting…")
        self._backup_worker = _BackupWorker(self.settings, path)
        self._backup_worker.progress.connect(self._on_backup_progress)
        self._backup_worker.finished.connect(self._on_backup_done)
        self._backup_worker.start()

    def _on_backup_progress(self, message):
        self._backup_status.setText(message)

    def _on_backup_done(self, result):
        self._backup_status.setText(result)
        self._backup_button.setEnabled(True)
        tone = "ok" if "failed" not in result.lower() else "bad"
        self.toast.show_message(result, tone)

    def _build_report_group(self):
        card, form = self._card_form("Appearance and reporting",
                                     "Theme, export options and punctuality rules.", icon="sliders")
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
        return card

    def _build_telegram_group(self):
        card, form = self._card_form("Telegram Notifications",
                                     "Real-time alerts for attendance and unknown faces.", icon="send")
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
        self.tg_test_button = QPushButton("Verify Bot && Send Test")
        self.tg_test_button.setObjectName("softButton")
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
        return card

    def _build_security_group(self):
        card, form = self._card_form("Security",
                                     "Dashboard access PIN and authentication.", icon="shield")
        self.pin_edit = QLineEdit()
        self.pin_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_edit.setPlaceholderText("Enter new PIN (leave empty to disable)")
        self.pin_confirm_edit = QLineEdit()
        self.pin_confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_confirm_edit.setPlaceholderText("Confirm PIN")
        self.pin_set_button = QPushButton("Set PIN")
        self.pin_set_button.setObjectName("softButton")
        self.pin_clear_button = QPushButton("Remove PIN")
        self.pin_clear_button.setObjectName("danger")
        self.pin_status = QLabel(
            "PIN is set" if self.settings.dashboard_pin_hash else "No PIN configured")
        self.pin_status.setObjectName("screenSubtitle")
        form.addRow("Dashboard PIN", self.pin_edit)
        form.addRow("Confirm PIN", self.pin_confirm_edit)
        row = QHBoxLayout()
        row.addWidget(self.pin_set_button)
        row.addWidget(self.pin_clear_button)
        row.addWidget(self.pin_status)
        row.addStretch(1)
        container = QWidget()
        container.setLayout(row)
        form.addRow("", container)
        note = QLabel("The PIN protects the dashboard on startup. "
                      "A SHA-256 hash is stored in settings.json, never the PIN itself.")
        note.setObjectName("screenSubtitle")
        note.setWordWrap(True)
        form.addRow("", note)
        self.pin_set_button.clicked.connect(self._set_pin)
        self.pin_clear_button.clicked.connect(self._clear_pin)
        return card

    def _build_backup_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)

        # ── overview stat cards ──────────────────────────────────────────
        overview = Card("Database Overview",
                        "Current connection and live statistics.",
                        icon="database", theme=self._theme)
        self._cards.append(overview)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self._bk_backend = StatCard("Backend", "—", "waiting…",
                                    icon="database", tone="blue", theme=self._theme)
        self._bk_tables = StatCard("Tables", "—", "waiting…",
                                   icon="list", tone="green", theme=self._theme)
        self._bk_records = StatCard("Total Rows", "—", "waiting…",
                                    icon="chart", tone="orange", theme=self._theme)
        self._bk_size = StatCard("Size", "—", "waiting…",
                                 icon="folder", tone="purple", theme=self._theme)
        self._backup_stats = [self._bk_backend, self._bk_tables,
                              self._bk_records, self._bk_size]
        for sc in self._backup_stats:
            stats_row.addWidget(sc, 1)
        overview.add_layout(stats_row)
        column.addWidget(overview)

        # ── export card ──────────────────────────────────────────────────
        export_card = Card("Export Backup",
                           "Create a portable SQL dump of your attendance data.",
                           icon="download", theme=self._theme)
        self._cards.append(export_card)

        panel = QFrame()
        panel.setObjectName("subtlePanel")
        panel_lay = QVBoxLayout(panel)
        panel_lay.setContentsMargins(16, 14, 16, 14)
        panel_lay.setSpacing(10)

        title = QLabel("SQL Dump (.sql)")
        title.setObjectName("sectionTitle")
        panel_lay.addWidget(title)

        desc = QLabel(
            "Standard INSERT statements for all attendance tables — "
            "attendance, cooldowns, outbox and audit log. Compatible with "
            "SQLite, PostgreSQL and MySQL.\n"
            "Settings and Telegram tokens are never included.")
        desc.setObjectName("screenSubtitle")
        desc.setWordWrap(True)
        panel_lay.addWidget(desc)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        self._backup_button = QPushButton("  Export as .sql")
        self._backup_button.setObjectName("primary")
        self._backup_status = QLabel("")
        self._backup_status.setObjectName("screenSubtitle")
        self._backup_status.setWordWrap(True)
        btn_row.addWidget(self._backup_button)
        btn_row.addWidget(self._backup_status, 1)
        panel_lay.addLayout(btn_row)

        export_card.add(panel)
        column.addWidget(export_card)

        column.addStretch(1)
        scroll.setWidget(holder)

        self._backup_button.clicked.connect(self._export_backup)
        return scroll

    def _set_pin(self):
        pin = self.pin_edit.text()
        confirm = self.pin_confirm_edit.text()
        if not pin:
            self.toast.show_message("Enter a PIN first.", "warn")
            return
        if len(pin) < 4:
            self.toast.show_message("PIN must be at least 4 characters.", "warn")
            return
        if pin != confirm:
            self.toast.show_message("PINs do not match.", "bad")
            return
        self.settings.dashboard_pin_hash = hash_pin(pin)
        try:
            self.settings.save()
        except OSError as exc:
            self.toast.show_message(f"Could not save: {exc}", "bad")
            return
        self.pin_edit.clear()
        self.pin_confirm_edit.clear()
        self.pin_status.setText("PIN is set")
        self.toast.show_message("Dashboard PIN saved. It takes effect on next launch.", "ok")
        self.settingsApplied.emit(self.settings, False)

    def _clear_pin(self):
        confirmed = QMessageBox.question(
            self, "Remove PIN",
            "Remove the dashboard PIN? Anyone with access to this machine will be able to "
            "open the dashboard without authentication.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        self.settings.dashboard_pin_hash = ""
        try:
            self.settings.save()
        except OSError as exc:
            self.toast.show_message(f"Could not save: {exc}", "bad")
            return
        self.pin_status.setText("No PIN configured")
        self.toast.show_message("Dashboard PIN removed.", "ok")
        self.settingsApplied.emit(self.settings, False)

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
        if hasattr(self, "updates_page"):
            self.updates_page.set_settings(settings)
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
        self._db_backend_changed()
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
        self._theme = theme
        self.toast.set_theme(theme)
        for card in self._cards:
            card.set_theme(theme)
        for sc in self._backup_stats:
            sc.set_theme(theme)
        self.updates_page.set_theme(theme)
        self._tab_strip.style().unpolish(self._tab_strip)
        self._tab_strip.style().polish(self._tab_strip)
        self._update_tab_icons()
        self._apply_icons()

    def _apply_icons(self):
        colors = palette(self._theme)
        apply_button_icon(self.apply_button, "check", colors["muted"])
        apply_button_icon(self.apply_restart_button, "restart", "#FFFFFF")
        apply_button_icon(self.reset_button, "refresh", colors["danger"])
        apply_button_icon(self._backup_button, "download", "#FFFFFF")


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


class _DbTestWorker(QThread):
    finished = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self._cfg = cfg

    def run(self):
        try:
            from ...database import connect
            conn = connect(self._cfg)
            conn.integrity_check()
            tables = []
            for t in ("attendance", "attendance_cooldowns", "attendance_outbox", "audit_log", "users"):
                if conn.table_exists(t):
                    tables.append(t)
            conn.close()
            backend = self._cfg.db_backend.upper()
            if tables:
                self.finished.emit(
                    f"Success! Connected to {backend} at {self._cfg.db_host}:{self._cfg.db_port}. "
                    f"Found {len(tables)} table(s).")
            else:
                self.finished.emit(
                    f"Success! Connected to {backend} at {self._cfg.db_host}:{self._cfg.db_port}. "
                    f"No tables yet — they will be created on first engine start.")
        except Exception as exc:
            self.finished.emit(f"Connection failed: {exc}")


class _BackupScanWorker(QThread):
    finished = pyqtSignal(object)

    def __init__(self, settings):
        super().__init__()
        self._settings = settings

    def run(self):
        from pathlib import Path as _P
        info = {"backend_label": "—", "backend_hint": "", "table_count": 0,
                "total_rows": 0, "size_str": "—", "size_hint": ""}
        backend = getattr(self._settings, "db_backend", "sqlite")

        if backend == "sqlite":
            info["backend_label"] = "SQLite"
            db_path = _P(str(getattr(self._settings, "db_path", "attendance.db")))
            info["backend_hint"] = db_path.name
            if db_path.exists():
                sz = db_path.stat().st_size
                info["size_str"] = self._fmt(sz)
                info["size_hint"] = "database file"
            else:
                info["size_str"] = "N/A"
                info["size_hint"] = "file not found"
                self.finished.emit(info)
                return
        else:
            info["backend_label"] = backend.upper()
            host = getattr(self._settings, "db_host", "")
            port = getattr(self._settings, "db_port", 0)
            info["backend_hint"] = f"{host}:{port}"
            info["size_hint"] = "remote"

        try:
            from ...database import connect
            conn = connect(self._settings, readonly=True)
            tables = ["attendance", "attendance_cooldowns", "attendance_outbox", "audit_log", "users"]
            existing = [t for t in tables if conn.table_exists(t)]
            info["table_count"] = len(existing)

            total = 0
            for t in existing:
                row = conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()
                if row:
                    total += int(row["c"])
            info["total_rows"] = total

            if backend == "mysql":
                row = conn.execute(
                    "SELECT SUM(data_length + index_length) AS s "
                    "FROM information_schema.tables WHERE table_schema = ?",
                    (getattr(self._settings, "db_name", ""),)).fetchone()
                if row and row["s"]:
                    info["size_str"] = self._fmt(float(row["s"]))
                    info["size_hint"] = "data + index"
            elif backend == "postgresql":
                row = conn.execute(
                    "SELECT pg_database_size(current_database()) AS s").fetchone()
                if row and row["s"]:
                    info["size_str"] = self._fmt(float(row["s"]))
                    info["size_hint"] = "total database"
            conn.close()
        except Exception:
            pass

        self.finished.emit(info)

    @staticmethod
    def _fmt(size):
        if size >= 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / 1024:.1f} KB"


class _BackupWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(str)

    def __init__(self, settings, output_path):
        super().__init__()
        self._settings = settings
        self._path = output_path

    def run(self):
        from datetime import datetime as _dt
        from pathlib import Path as _P
        try:
            from ...database import connect
            conn = connect(self._settings, readonly=True)
            backend = conn.backend

            tables = ["attendance", "attendance_cooldowns", "attendance_outbox", "audit_log", "users"]
            existing = [t for t in tables if conn.table_exists(t)]

            lines = [
                "-- ═══════════════════════════════════════════════════════════",
                "-- SV Face Attendance — Database Backup",
                f"-- Backend : {backend}",
                f"-- Date    : {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"-- Tables  : {', '.join(existing) or '(none)'}",
                "-- ═══════════════════════════════════════════════════════════",
                "",
            ]

            total_rows = 0
            for table in existing:
                self.progress.emit(f"Exporting {table}…")
                columns = sorted(conn.get_columns(table))
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()

                lines.append(f"-- Table: {table} ({len(rows)} rows)")
                lines.append("")

                for row in rows:
                    values = []
                    for col in columns:
                        try:
                            val = row[col]
                        except (KeyError, IndexError):
                            values.append("NULL")
                            continue
                        if val is None:
                            values.append("NULL")
                        elif isinstance(val, (int, float)):
                            values.append(str(val))
                        else:
                            escaped = str(val).replace("\\", "\\\\").replace("'", "''")
                            values.append(f"'{escaped}'")
                    cols_str = ", ".join(columns)
                    vals_str = ", ".join(values)
                    lines.append(f"INSERT INTO {table} ({cols_str}) VALUES ({vals_str});")

                lines.append("")
                total_rows += len(rows)

            conn.close()

            with open(self._path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            size = _P(self._path).stat().st_size
            if size > 1024 * 1024:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            elif size > 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} bytes"

            self.finished.emit(
                f"Exported {total_rows} rows from {len(existing)} tables ({size_str})")
        except Exception as exc:
            self.finished.emit(f"Backup failed: {exc}")
