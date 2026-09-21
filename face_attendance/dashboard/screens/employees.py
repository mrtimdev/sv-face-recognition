"""Employees: enrollment by live capture or by photo upload, plus catalog edits.

Encoding is CPU work, so it runs on a short-lived worker thread and reports
back through a Qt signal. The catalog is reloaded into the engine afterwards,
which is why enrollment pauses recording and restarts the engine on save.
"""
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QSpinBox, QTabWidget, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from ...camera import CameraManager
from ...enrollment import EnrollmentOutcome, EnrollmentService, FACE_BACKEND_LOCK
from ..bridge import to_pixmap
from ..widgets import EmptyState, StatCard, ToastBar, VideoView


PHOTO_FILTER = "Images (*.jpg *.jpeg *.png *.bmp *.tiff)"
TABLE_HEADERS = ("Label", "Employee ID", "Display name", "Samples", "Mapped")
SAMPLE_LIMIT = 8
AUTO_CAPTURE_COOLDOWN = 1.5   # seconds between automatic captures
PREVIEW_INTERVAL_MS = 33      # ~30 fps preview refresh from the dedicated camera


class EnrollmentRunner(QObject):
    """Encodes samples off the Qt thread; the widgets stay responsive."""

    finished = pyqtSignal(object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service

    def submit(self, images, name, employee_id, check_quality=True):
        copies = [image.copy() for image in images if image is not None]
        thread = threading.Thread(target=self._work,
                                  args=(copies, name, employee_id, check_quality),
                                  name="enrollment", daemon=True)
        thread.start()

    def _work(self, images, name, employee_id, check_quality):
        try:
            outcome = self.service.enroll_many(images, name, employee_id, check_quality)
        except Exception as exc:  # Never lose the dialog state to a backend fault.
            outcome = EnrollmentOutcome("error", str(exc), name=name, employee_id=employee_id or "")
        self.finished.emit(outcome)


class SampleRunner(QObject):
    """Validates one prospective sample off the Qt thread.

    ``inspect`` runs a HOG face detection, which takes hundreds of
    milliseconds per frame; doing that on the UI thread froze the preview and
    made capture feel broken. The validated frame is returned with the result
    so the appended sample is exactly the image that was checked.
    """

    validated = pyqtSignal(object, object)   # ((status, message, issues), frame)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._busy = False

    @property
    def busy(self):
        return self._busy

    def submit(self, frame, check_quality):
        if self._busy or frame is None:
            return False
        self._busy = True
        thread = threading.Thread(target=self._work, args=(frame.copy(), check_quality),
                                  name="enroll-sample", daemon=True)
        thread.start()
        return True

    def _work(self, frame, check_quality):
        try:
            status, message, issues = self.service.inspect(frame, check_quality)
        except Exception as exc:  # Keep the wizard alive on backend faults.
            status, message, issues = "error", str(exc), ()
        self.validated.emit((status, message, issues), frame)
        self._busy = False


class DropLabel(QLabel):
    """Photo drop target: reuses the same validation as the file dialog."""

    dropped = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(220)
        self.setObjectName("panel")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.dropped.emit(url.toLocalFile())
                break
        event.acceptProposedAction()


class EmployeesScreen(QWidget):
    probeReady = pyqtSignal(object)   # (status, message, box) from the detection thread

    def __init__(self, engine, settings, parent=None, preview_capture_factory=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._preview_factory = preview_capture_factory
        self.service = EnrollmentService(settings.encodings_path, settings.employees_path)
        self.runner = EnrollmentRunner(self.service, self)
        self.pending = []      # BGR samples waiting to be written
        self.upload = None      # BGR array chosen in the Upload tab
        self.rows = []
        self.countdown = 0
        self.probing = False
        self.validating = False
        self._sample_source = "live"   # which tab requested the running validation
        self.sample_runner = SampleRunner(self.service, self)
        self.last_auto_capture = 0.0
        self.preview_on = False
        self.preview_camera = None   # dedicated capture; never the engine's camera
        self._engine_was_running = False
        self._build()
        self._connect()
        self.reload()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("Employees")
        title.setObjectName("screenTitle")
        self.subtitle = QLabel("")
        self.subtitle.setObjectName("screenSubtitle")
        titles.addWidget(title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles)
        header.addStretch(1)
        self.reload_button = QPushButton("Reload list")
        self.folder_button = QPushButton("Open data folder")
        header.addWidget(self.reload_button)
        header.addWidget(self.folder_button)
        layout.addLayout(header)

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(8)
        self.table = QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        left.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.use_button = QPushButton("Add samples to selected")
        self.remove_sample_button = QPushButton("Delete newest sample")
        self.remove_button = QPushButton("Remove employee")
        self.remove_button.setObjectName("danger")
        for button in (self.use_button, self.remove_sample_button, self.remove_button):
            actions.addWidget(button)
        actions.addStretch(1)
        left.addLayout(actions)
        self.notes = QLabel("Removing enrollment never deletes attendance history: the rows keep "
                            "their recorded name and employee ID.")
        self.notes.setObjectName("screenSubtitle")
        self.notes.setWordWrap(True)
        left.addWidget(self.notes)
        body.addLayout(left, 3)
        body.addWidget(self._build_wizard(), 2)
        layout.addLayout(body, 1)

        self.toast = ToastBar()
        layout.addWidget(self.toast)

    def _build_wizard(self):
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        heading = QLabel("Enroll an employee")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        form = QGridLayout()
        form.setSpacing(6)
        form.addWidget(QLabel("Display name"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Required, e.g. Jane Smith")
        form.addWidget(self.name_edit, 0, 1)
        form.addWidget(QLabel("Employee ID"), 1, 0)
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("Optional, e.g. EMP-125")
        form.addWidget(self.id_edit, 1, 1)
        self.quality_check = QCheckBox("Reject tiny, blurred or badly lit samples")
        self.quality_check.setChecked(True)
        form.addWidget(self.quality_check, 2, 0, 1, 2)
        layout.addLayout(form)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_capture_tab(), "Live capture")
        self.tabs.addTab(self._build_upload_tab(), "Upload photo")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs, 1)

        layout.addWidget(QLabel("Samples for this save"))
        self.samples_list = QListWidget()
        self.samples_list.setMaximumHeight(110)
        layout.addWidget(self.samples_list)
        buttons = QHBoxLayout()
        self.clear_button = QPushButton("Clear samples")
        self.save_button = QPushButton("Save enrollment")
        self.save_button.setObjectName("primary")
        buttons.addWidget(self.clear_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.hint = QLabel("One employee per sample; images with several faces are rejected. "
                           "Saving restarts the engine so new samples apply immediately.")
        self.hint.setObjectName("screenSubtitle")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        return panel

    def _build_capture_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.capture_video = VideoView(tab, message="Preview off",
                                       subtitle="Start the preview to capture samples")
        layout.addWidget(self.capture_video, 1)
        self.coach = QLabel("Type the display name, start the preview, then capture samples.")
        self.coach.setObjectName("screenSubtitle")
        self.coach.setWordWrap(True)
        layout.addWidget(self.coach)
        row = QHBoxLayout()
        self.preview_button = QPushButton("Start preview")
        self.capture_button = QPushButton("Capture sample")
        self.capture_button.setObjectName("primary")
        self.capture_button.setEnabled(False)
        self.auto_check = QCheckBox("Auto-capture")
        self.auto_check.setToolTip("Capture automatically while exactly one readable face is in view")
        self.countdown_box = QSpinBox()
        self.countdown_box.setRange(0, 10)
        self.countdown_box.setValue(3)
        self.countdown_box.setSuffix("s")
        row.addWidget(self.preview_button)
        row.addWidget(self.capture_button)
        row.addWidget(self.auto_check)
        row.addWidget(self.countdown_box)
        row.addStretch(1)
        layout.addLayout(row)
        return tab

    def _build_upload_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.drop = DropLabel("Drop a photo here\nor choose one below")
        self.drop.setObjectName("panel")
        layout.addWidget(self.drop, 1)
        row = QHBoxLayout()
        self.choose_button = QPushButton("Choose photo")
        self.add_upload_button = QPushButton("Add photo as sample")
        self.add_upload_button.setObjectName("primary")
        self.add_upload_button.setEnabled(False)
        row.addWidget(self.choose_button)
        row.addWidget(self.add_upload_button)
        row.addStretch(1)
        layout.addLayout(row)
        return tab

    def _connect(self):
        self.engine.catalogChanged.connect(lambda rows: self.reload())
        self.runner.finished.connect(self.on_enrollment_finished)
        self.sample_runner.validated.connect(self.on_sample_validated)
        self.probeReady.connect(self.on_probe)
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(PREVIEW_INTERVAL_MS)
        self.preview_timer.timeout.connect(self._pull_preview)
        self.preview_button.clicked.connect(self._toggle_preview)
        self.capture_button.clicked.connect(self._start_countdown)
        self.clear_button.clicked.connect(self._clear_samples)
        self.save_button.clicked.connect(self._save)
        self.choose_button.clicked.connect(self._choose_photo)
        self.drop.dropped.connect(self._load_photo)
        self.add_upload_button.clicked.connect(self._add_upload_sample)
        self.reload_button.clicked.connect(self.reload)
        self.folder_button.clicked.connect(self._open_folder)
        self.use_button.clicked.connect(self._use_selected)
        self.remove_sample_button.clicked.connect(self._remove_sample)
        self.remove_button.clicked.connect(self._remove_employee)
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._tick_countdown)
        self.probe_timer = QTimer(self)
        self.probe_timer.setInterval(700)
        self.probe_timer.timeout.connect(self._probe_now)
        self._tab_changed(0)

    def _tab_changed(self, index):
        capture = index == 0
        if capture and self.preview_on:
            self.probe_timer.start()
        else:
            self.probe_timer.stop()
        if not capture and self.preview_on:
            self._toggle_preview()  # Leaving the tab closes the dedicated camera.

    # --- live capture (a dedicated camera, independent of the engine) ------
    def on_frame(self, frame):
        """Unused: the preview pulls frames from its own camera, not the engine."""

    def _latest_preview_frame(self):
        packet = self.preview_camera.frames.get() if self.preview_camera is not None else None
        return None if packet is None else packet.frame

    def _pull_preview(self):
        if not self.preview_on:
            return
        frame = self._latest_preview_frame()
        if frame is not None:
            self.capture_video.set_frame(frame)

    def _toggle_preview(self):
        if self.preview_on:
            self._stop_preview()
            return
        self._start_preview()

    def _start_preview(self):
        try:
            config = self.settings.to_config()
        except ValueError as exc:
            self.toast.show_message(f"Camera settings are invalid: {exc}", "bad")
            return
        # The enrollment camera needs the device: pause attendance if it is
        # recording, and resume it automatically when the preview closes.
        self._engine_was_running = self.engine.running
        if self._engine_was_running:
            try:
                self.engine.stop()
            except Exception as exc:
                self._engine_was_running = False
                self.toast.show_message(f"Could not pause attendance: {exc}", "bad")
                return
            self.toast.show_message("Attendance paused while the enrollment camera is open; "
                                    "it resumes when the preview closes.", "warn")
        self.preview_camera = CameraManager(config, capture_factory=self._preview_factory)
        self.preview_camera.start()
        self.preview_on = True
        self.preview_button.setText("Stop preview")
        self.capture_button.setEnabled(True)
        self.preview_timer.start()
        self.probe_timer.start()
        self.coach.setText("Preview running on the enrollment camera. "
                           "Face the camera, then capture samples.")

    def _stop_preview(self, resume=True):
        self.preview_on = False
        self.probe_timer.stop()
        self.preview_timer.stop()
        if self.preview_camera is not None:
            self.preview_camera.close()
            self.preview_camera = None
        self.preview_button.setText("Start preview")
        self.capture_button.setEnabled(False)
        self.capture_video.show_message("Preview off", "Start the preview to capture samples")
        self.coach.setText("Preview stopped.")
        if resume and self._engine_was_running:
            self._engine_was_running = False
            try:
                self.engine.start()
                self.toast.show_message("Attendance resumed.", "ok")
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                self.toast.show_message(f"Preview closed, but attendance could not "
                                        f"restart: {exc}", "warn")
        else:
            self._engine_was_running = False

    def _probe_now(self):
        """Detect one face off the Qt thread so the preview keeps flowing."""
        if (self.probing or self.validating or not self.preview_on
                or self.preview_camera is None):
            return
        frame = self._latest_preview_frame()
        if frame is None:
            return
        self.probing = True
        threading.Thread(target=self._probe, args=(frame.copy(),), name="enroll-probe",
                         daemon=True).start()

    def _probe(self, frame):
        scale = max(0.1, float(self.settings.detection_scale))
        try:
            small = cv2.resize(frame, (max(1, round(frame.shape[1] * scale)),
                                       max(1, round(frame.shape[0] * scale))),
                               interpolation=cv2.INTER_AREA)
            with FACE_BACKEND_LOCK:  # dlib's global detector is not thread-safe
                boxes = self.service._detector().face_locations(
                    cv2.cvtColor(small, cv2.COLOR_BGR2RGB), model="hog")
        except Exception as exc:
            self.probeReady.emit(("error", f"Face detection failed: {exc}", None, (), None))
            return
        if not boxes:
            self.probeReady.emit(("no_face", "No face detected. Step into the frame.", None, (), None))
            return
        if len(boxes) > 1:
            self.probeReady.emit(("multiple_faces",
                                  f"{len(boxes)} faces in view. Enroll one person per sample.",
                                  None, (), None))
            return
        top, right, bottom, left = boxes[0]
        box = (top / scale, right / scale, bottom / scale, left / scale)
        issues = ()
        if self.quality_check.isChecked():
            from ...enrollment import frame_quality
            issues = frame_quality(frame, box)
        self.probeReady.emit(("ok", "", box, issues, frame))

    def on_probe(self, payload):
        self.probing = False
        status, message, _box, issues, frame = payload
        if status != "ok":
            self.coach.setText(message)
            return
        if issues:
            self.coach.setText("Hold on: " + "; ".join(issues))
            return
        self.coach.setText("Face ready. Capture a sample" +
                           (" (auto-capture is on)." if self.auto_check.isChecked() else "."))
        self._auto_capture(frame)

    def _auto_capture(self, frame):
        """Auto-capture with a cooldown so one visitor cannot fill the list."""
        if not self.auto_check.isChecked() or self.validating:
            return
        if len(self.pending) >= SAMPLE_LIMIT:
            return
        if time.monotonic() - self.last_auto_capture < AUTO_CAPTURE_COOLDOWN:
            return
        self.last_auto_capture = time.monotonic()
        self._submit_sample(frame)

    def _start_countdown(self):
        if self.countdown:
            return
        seconds = self.countdown_box.value()
        if seconds <= 0:
            self._grab_sample()
            return
        self.countdown = seconds
        self.capture_button.setEnabled(False)
        self._tick_countdown()
        self.countdown_timer.start()

    def _tick_countdown(self):
        if self.countdown <= 0:
            self.countdown_timer.stop()
            self.capture_button.setEnabled(self.preview_on)
            self.capture_button.setText("Capture sample")
            return
        self.capture_button.setText(f"Capturing in {self.countdown}...")
        if self.countdown == 1:
            self.countdown = 0
            self.countdown_timer.stop()
            self.capture_button.setEnabled(self.preview_on)
            self.capture_button.setText("Capture sample")
            self._grab_sample()
            return
        self.countdown -= 1

    def _grab_sample(self):
        """Manual capture: validation runs off the UI thread, so the preview
        keeps flowing while the face detector works on the grabbed frame."""
        if self.validating or not self.preview_on:
            return
        frame = self._latest_preview_frame()
        if frame is None:
            self.coach.setText("No camera frame available yet.")
            return
        self.capture_button.setEnabled(False)
        self.capture_button.setText("Checking...")
        self._submit_sample(frame)

    def _submit_sample(self, frame):
        self._sample_source = "live"
        if self.sample_runner.submit(frame, self.quality_check.isChecked()):
            self.validating = True

    def on_sample_validated(self, result, frame):
        self.validating = False
        status, message, issues = result
        upload_mode = self._sample_source == "upload"
        if upload_mode:
            self.add_upload_button.setText("Add photo as sample")
            self.add_upload_button.setEnabled(True)
        else:
            self.capture_button.setText("Capture sample")
            self.capture_button.setEnabled(self.preview_on and not self.countdown)
        if status != "ok":
            text = message or "Sample rejected."
            if issues:
                text += " (" + "; ".join(issues) + ")"
            self.coach.setText(text)
            self.toast.show_message(text, "warn")
            return
        self.pending.append(frame)
        self._refresh_samples()
        if upload_mode:
            # Let the user pick a different photo next: the chosen one is now
            # a pending sample, so the preview is cleared instead of lingering.
            self.upload = None
            self.drop.clear()
            self.drop.setText("Drop a photo here\nor choose one below")
            self.add_upload_button.setEnabled(False)
            self.coach.setText(f"Photo added as sample {len(self.pending)}. "
                               "Choose another photo or switch to Live capture.")
            self.toast.show_message(f"Photo added as sample {len(self.pending)}", "ok")
        else:
            self.coach.setText(f"Sample {len(self.pending)} added. Vary the angle and capture more, "
                               "then save.")
            self.toast.show_message(f"Sample {len(self.pending)} captured", "ok")

    # --- upload -----------------------------------------------------------
    def _choose_photo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a photo", str(Path.home()), PHOTO_FILTER)
        if path:
            self._load_photo(path)

    def _load_photo(self, path):
        image = cv2.imread(path)
        if image is None:
            self.toast.show_message(f"Could not read {path}", "bad")
            return
        self.upload = image
        pixmap = to_pixmap(image)
        self.drop.setPixmap(pixmap.scaled(self.drop.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        self.add_upload_button.setEnabled(True)
        self.drop.setToolTip(path)

    def _add_upload_sample(self):
        if self.upload is None:
            self.toast.show_message("Choose a photo first.", "warn")
            return
        if self.validating:
            self.toast.show_message("A sample check is already running.", "warn")
            return
        # Validation runs HOG detection: keep it off the UI thread so the
        # window stays responsive even while attendance recognition is running.
        self._sample_source = "upload"
        self.add_upload_button.setEnabled(False)
        self.add_upload_button.setText("Checking photo...")
        self.validating = True
        self.sample_runner.submit(self.upload, self.quality_check.isChecked())

    # --- samples and saving ----------------------------------------------
    def _refresh_samples(self):
        self.samples_list.clear()
        for index, sample in enumerate(self.pending, start=1):
            height, width = sample.shape[:2]
            item = QListWidgetItem(f"Sample {index} - {width}x{height}")
            item.setIcon(QIcon(to_pixmap(sample).scaled(72, 54, Qt.AspectRatioMode.KeepAspectRatio,
                                                       Qt.TransformationMode.SmoothTransformation)))
            self.samples_list.addItem(item)
        self.save_button.setEnabled(bool(self.pending))

    def _clear_samples(self):
        self.pending.clear()
        self.upload = None
        self.drop.clear()
        self.drop.setText("Drop a photo here\nor choose one below")
        self.add_upload_button.setEnabled(False)
        self._refresh_samples()
        self.coach.setText("Samples cleared.")

    def _save(self):
        name = self.name_edit.text().strip()
        if not name:
            self.toast.show_message("Enter the employee display name.", "warn")
            return
        if not self.pending:
            self.toast.show_message("Capture or upload at least one sample.", "warn")
            return
        self.save_button.setEnabled(False)
        self.save_button.setText("Saving...")
        self.toast.show_message("Encoding samples...", "info")
        self.runner.submit(self.pending, name, self.id_edit.text().strip(),
                           self.quality_check.isChecked())

    def on_enrollment_finished(self, outcome):
        self.save_button.setText("Save enrollment")
        self.save_button.setEnabled(bool(self.pending))
        if not outcome.ok:
            self.toast.show_message(f"{outcome.message}", "bad")
            return
        self.pending.clear()
        self._refresh_samples()
        self.toast.show_message(f"{outcome.message}. Total samples: {outcome.total_samples}", "ok")
        self.name_edit.clear()
        self.id_edit.clear()
        self.reload()
        self.engine.reload_catalog()

    # --- catalog management ----------------------------------------------
    def reload(self):
        self.rows = self.service.employees()
        self.table.setRowCount(len(self.rows))
        self.table.setSortingEnabled(False)
        for index, row in enumerate(self.rows):
            values = (row["name"], row["employee_id"], row["employee_name"],
                      row["samples"], "yes" if row["mapped"] else "derived")
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        total = sum(row["samples"] for row in self.rows)
        people = len({row["employee_id"] for row in self.rows})
        self.subtitle.setText(f"{len(self.rows)} enrollment label(s), {people} employee ID(s), "
                              f"{total} sample(s) in {Path(self.settings.encodings_path).name}")
        self._selection_changed()

    def selected_row(self):
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not indexes or not 0 <= indexes[0].row() < len(self.rows):
            return None
        return self.rows[indexes[0].row()]

    def _selection_changed(self):
        row = self.selected_row()
        self.use_button.setEnabled(row is not None)
        self.remove_sample_button.setEnabled(row is not None and row["samples"] > 0)
        self.remove_button.setEnabled(row is not None)

    def _use_selected(self):
        row = self.selected_row()
        if row is None:
            return
        self.name_edit.setText(row["name"])
        self.id_edit.setText(row["employee_id"])
        self.tabs.setCurrentIndex(0)
        self.toast.show_message(f"Adding samples to {row['name']!r} "
                                f"({row['samples']} existing). Capture or upload, then save.", "info")

    def _remove_sample(self):
        row = self.selected_row()
        if row is None:
            return
        try:
            removed = self.service.delete_sample(row["name"], max(0, row["samples"] - 1))
        except (ValueError, OSError) as exc:
            self.toast.show_message(str(exc), "bad")
            return
        self.reload()
        self.engine.reload_catalog()
        self.toast.show_message(f"Removed one sample from {row['name']!r} "
                                f"({removed} encoding(s) deleted)", "ok")

    def _remove_employee(self):
        row = self.selected_row()
        if row is None:
            return
        confirmed = QMessageBox.question(
            self, "Remove enrollment",
            f"Remove all {row['samples']} enrolled sample(s) for {row['name']!r}?\n\n"
            "Attendance history is kept: existing rows keep their recorded name and ID. "
            "The engine restarts so the change applies immediately.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.service.delete_employee(row["name"])
        except (ValueError, OSError) as exc:
            self.toast.show_message(str(exc), "bad")
            return
        self.reload()
        self.engine.reload_catalog()
        self.toast.show_message(f"Removed {row['name']!r} ({removed} encoding(s)). "
                                "Attendance history is unchanged.", "ok")

    def _open_folder(self):
        folder = Path(self.settings.encodings_path).parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def on_settings_changed(self, settings):
        self.settings = settings
        self.service = EnrollmentService(settings.encodings_path, settings.employees_path)
        self.runner.service = self.service
        if self.preview_on:
            self._stop_preview()  # The source may have changed; drop the old capture.
        self.reload()

    def closeEvent(self, event):
        self.probe_timer.stop()
        self.countdown_timer.stop()
        self.preview_timer.stop()
        if self.preview_on:
            self._stop_preview(resume=False)  # The window is going away.
        # Worker threads may still be running; delivering their queued signal
        # payloads into a destroyed widget aborts PyQt, so drop the
        # cross-thread connections before the screen goes away.
        for signal in (self.probeReady, self.runner.finished, self.sample_runner.validated):
            try:
                signal.disconnect()
            except TypeError:
                pass
        super().closeEvent(event)

    def set_theme(self, theme):
        self.capture_video.set_theme(theme)