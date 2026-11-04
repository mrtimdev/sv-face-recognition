"""AI Face Angles: generate six 1:1 head angles of one person from a single photo.

The person is an enrolled employee or a new one; the photo is their enrolled
photo or an upload. Images are saved as they arrive. Nothing joins the face
catalog unless the user asks, and then only through the normal enrollment checks.
"""
import logging
import pickle
from pathlib import Path

import cv2
from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QCompleter, QFileDialog, QFrame, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                             QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from ...ai_providers import (AIError, ANGLES, angles_directory, config_from_settings, configuration_problem,
                             encode_source, generate_angle_file, provider_host)
from ...enrollment import EnrollmentService
from ...enrollment_photos import portrait_path, sample_photo_map
from ...storage import read_image
from ..access import guard
from ..bridge import to_pixmap
from ..icons import apply_button_icon
from ..theme import palette
from ..threads import start_detached
from ..widgets import Card, PageHeader, ToastBar
from .employees import DropLabel, EnrollmentRunner, NameFields, PHOTO_FILTER

TILE_IMAGE_SIZE = 150
PREVIEW_SIZE = (296, 220)
EXISTING, NEW = 0, 1
STOP_ON_STATUS = (401, 402, 403)  # The remaining angles would fail the same way.
CATALOG_ERRORS = (OSError, ValueError, EOFError, AttributeError, pickle.UnpicklingError)


class AngleWorker(QThread):
    """Generates angles one at a time, so Stop never wastes more than the current image."""

    angleStarted = pyqtSignal(str)
    angleDone = pyqtSignal(str, str)
    angleFailed = pyqtSignal(str, str)
    runDone = pyqtSignal(bool)  # True when the run ended early

    def __init__(self, config, source_jpeg, angles, directory):
        super().__init__()
        self._config = config
        self._source = source_jpeg
        self._angles = tuple(angles)
        self._directory = directory
        self._stopped = False

    def stop(self):
        self._stopped = True

    def run(self):
        for angle in self._angles:
            if self._stopped:
                break
            self.angleStarted.emit(angle.key)
            try:
                path = generate_angle_file(self._config, self._source, angle, self._directory)
            except AIError as exc:
                self.angleFailed.emit(angle.key, str(exc))
                if exc.status in STOP_ON_STATUS:
                    self._stopped = True
            except Exception:
                logging.exception("AI angle generation failed")
                self.angleFailed.emit(angle.key, "Something went wrong. See the log for details.")
            else:
                self.angleDone.emit(angle.key, str(path))
        self.runDone.emit(self._stopped)


class AngleTile(QFrame):
    regenerate = pyqtSignal(str)

    def __init__(self, angle, parent=None):
        super().__init__(parent)
        self.angle = angle
        self.path = None
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        title = QLabel(angle.label)
        title.setObjectName("cellTitle")
        layout.addWidget(title)
        self.image = QLabel()
        self.image.setFixedSize(TILE_IMAGE_SIZE, TILE_IMAGE_SIZE)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.image, 0, Qt.AlignmentFlag.AlignHCenter)
        self.status = QLabel()
        self.status.setObjectName("fieldHelp")
        self.status.setWordWrap(True)
        self.status.setMaximumWidth(TILE_IMAGE_SIZE + 8)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.include = QCheckBox("Use")
        self.include.setToolTip("Include this image when adding to enrollment")
        self.retry = QPushButton("Redo")
        self.retry.setToolTip("Generate this angle again")
        self.retry.clicked.connect(lambda: self.regenerate.emit(self.angle.key))
        row.addWidget(self.include)
        row.addStretch(1)
        row.addWidget(self.retry)
        layout.addLayout(row)
        layout.addStretch(1)
        self.reset()

    def _show(self, image_text, status, include_enabled=False):
        self.image.clear()
        self.image.setText(image_text)
        self.status.setText(status)
        self.status.setToolTip("")
        self.include.setChecked(include_enabled)
        self.include.setEnabled(include_enabled)

    def reset(self):
        self.path = None
        self._show("—", "Waiting")

    def set_working(self):
        self._show("…", "Generating…")

    def set_failed(self, message):
        self.path = None
        self._show("!", message)
        self.status.setToolTip(message)

    def set_result(self, path):
        pixmap = to_pixmap(image) if (image := read_image(path)) is not None else None
        if pixmap is None:
            self.set_failed("The saved image couldn't be read.")
            return
        self.path = path
        self._show("", "Ready", include_enabled=True)
        self.image.setPixmap(pixmap.scaled(TILE_IMAGE_SIZE, TILE_IMAGE_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))


class AIAnglesScreen(QWidget):
    openSettingsRequested = pyqtSignal()

    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._theme = getattr(settings, "theme", "dark")
        self.source = None
        self.output_dir = None
        self._worker = None
        self._saving = False
        self._done = 0
        self._cards = []
        self.runner = None
        self._make_service()
        self._build()
        self._reload_employees()
        self._refresh_provider()
        self._update_buttons()

    def _make_service(self):
        self.service = EnrollmentService(self.settings.encodings_path, self.settings.employees_path,
                                         duplicate_distance=self.settings.face_tolerance)
        if self.runner is None:
            self.runner = EnrollmentRunner(self.service, self)
            self.runner.finished.connect(self._on_enrolled)
        self.runner.service = self.service

    # --- construction -----------------------------------------------------
    def _card(self, title, subtitle, icon):
        card = Card(title, subtitle, icon=icon, theme=self._theme)
        self._cards.append(card)
        return card

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(14)
        self.header = PageHeader("", "Create six 1:1 head angles of one person from a single photo.")
        self.header.layout().setStretch(0, 1)
        self.settings_button = QPushButton("AI settings")
        self.settings_button.setObjectName("softButton")
        self.settings_button.clicked.connect(self.openSettingsRequested)
        self.header.add_action(self.settings_button)
        outer.addWidget(self.header)
        self.provider_label = QLabel()
        self.provider_label.setObjectName("screenSubtitle")
        self.provider_label.setWordWrap(True)
        outer.addWidget(self.provider_label)

        body = QHBoxLayout()
        body.setSpacing(14)
        left = QWidget()
        left.setObjectName("plain")
        left.setFixedWidth(340)
        left_column = QVBoxLayout(left)
        left_column.setContentsMargins(0, 0, 0, 0)
        left_column.setSpacing(14)
        left_column.addWidget(self._build_person_card())
        left_column.addWidget(self._build_photo_card())
        left_column.addStretch(1)
        self.left_panel = left
        body.addWidget(self._scrolled(left))
        right = QWidget()
        right.setObjectName("plain")
        right_column = QVBoxLayout(right)
        right_column.setContentsMargins(0, 0, 0, 0)
        right_column.addWidget(self._build_angles_card())
        right_column.addStretch(1)
        body.addWidget(self._scrolled(right), 1)
        outer.addLayout(body, 1)
        outer.addWidget(self.progress)
        outer.addLayout(self._build_actions())
        self.toast = ToastBar(theme=self._theme)
        outer.addWidget(self.toast)
        self._apply_icons()

    @staticmethod
    def _scrolled(widget):
        scroll = QScrollArea()
        scroll.setObjectName("plain")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(widget)
        if widget.minimumWidth() == widget.maximumWidth():
            scroll.setFixedWidth(widget.maximumWidth() + 12)
        return scroll

    def _build_person_card(self):
        card = self._card("1. Person", "Who are these images of?", "user")
        bar = QFrame()
        bar.setObjectName("segmentBar")
        modes = QHBoxLayout(bar)
        modes.setContentsMargins(3, 3, 3, 3)
        modes.setSpacing(2)
        self.existing_button = QPushButton("Existing employee")
        self.new_button = QPushButton("New employee")
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        for mode, button in ((EXISTING, self.existing_button), (NEW, self.new_button)):
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.mode_group.addButton(button, mode)
            modes.addWidget(button, 1)
        self.existing_button.setChecked(True)
        card.add(bar)

        self.person_stack = QStackedWidget()
        self.person_stack.setObjectName("plain")
        existing = QWidget()
        existing.setObjectName("plain")
        existing_layout = QVBoxLayout(existing)
        existing_layout.setContentsMargins(0, 0, 0, 0)
        self.employee_combo = QComboBox()
        self.employee_combo.setEditable(True)
        self.employee_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.employee_combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.employee_combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.employee_combo.lineEdit().setPlaceholderText("Search or choose an employee")
        existing_layout.addWidget(self.employee_combo)
        self.person_stack.addWidget(existing)

        new = QWidget()
        new.setObjectName("plain")
        grid = QGridLayout(new)
        grid.setContentsMargins(0, 0, 0, 0)
        self.names = NameFields(self)
        row = self.names.add_to(grid)
        self.id_edit = QLineEdit()
        self.id_edit.setMaxLength(100)
        self.id_edit.setPlaceholderText("Optional")
        grid.addWidget(QLabel("Employee ID"), row, 0)
        grid.addWidget(self.id_edit, row, 1)
        self.person_stack.addWidget(new)
        card.add(self.person_stack)
        self._fit_person_stack()

        self.mode_group.idClicked.connect(self._mode_changed)
        self.employee_combo.currentIndexChanged.connect(self._employee_changed)
        return card

    def _fit_person_stack(self):
        self.person_stack.setFixedHeight(self.person_stack.currentWidget().sizeHint().height())

    def _build_photo_card(self):
        card = self._card("2. Photo", "A clear, front-facing photo works best.", "image")
        self.preview = DropLabel()
        self.preview.setFixedHeight(PREVIEW_SIZE[1])
        self.preview.dropped.connect(self._load_photo)
        card.add(self.preview)
        row = QHBoxLayout()
        self.upload_button = QPushButton("Upload photo")
        self.upload_button.clicked.connect(self._choose_photo)
        self.enrolled_button = QPushButton("Use enrolled photo")
        self.enrolled_button.clicked.connect(lambda: self._use_enrolled_photo())
        row.addWidget(self.upload_button, 1)
        row.addWidget(self.enrolled_button, 1)
        card.add_layout(row)
        self.privacy_label = QLabel()
        self.privacy_label.setObjectName("fieldHelp")
        self.privacy_label.setWordWrap(True)
        card.add(self.privacy_label)
        self._show_source()
        return card

    def _build_angles_card(self):
        card = self._card("3. Six angles", "Each image is saved as soon as it arrives.", "face-id")
        grid = QGridLayout()
        grid.setSpacing(10)
        self.tiles = {}
        for index, angle in enumerate(ANGLES):
            tile = AngleTile(angle)
            tile.regenerate.connect(self._regenerate)
            tile.include.toggled.connect(lambda _on: self._update_buttons())
            self.tiles[angle.key] = tile
            grid.addWidget(tile, index // 3, index % 3)
        grid.setColumnStretch(3, 1)
        card.add_layout(grid)
        self.progress = QProgressBar()
        self.progress.setRange(0, len(ANGLES))
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress.hide()
        return card

    def _build_actions(self):
        actions = QHBoxLayout()
        self.generate_button = QPushButton("Generate 6 angles")
        self.generate_button.setObjectName("primary")
        self.generate_button.clicked.connect(self._generate_all)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._stop)
        self.stop_button.hide()
        self.folder_button = QPushButton("Open folder")
        self.folder_button.clicked.connect(self._open_folder)
        self.enroll_button = QPushButton("Add to enrollment")
        self.enroll_button.clicked.connect(self._add_to_enrollment)
        actions.addWidget(self.generate_button)
        actions.addWidget(self.stop_button)
        actions.addStretch(1)
        actions.addWidget(self.folder_button)
        actions.addWidget(self.enroll_button)
        return actions

    # --- state ------------------------------------------------------------
    @property
    def busy(self):
        return self._worker is not None

    def _update_buttons(self):
        busy = self.busy or self._saving
        self.left_panel.setEnabled(not busy)
        self.generate_button.setVisible(not self.busy)
        self.generate_button.setEnabled(not busy)
        self.stop_button.setVisible(self.busy)
        self.folder_button.setEnabled(any(tile.path for tile in self.tiles.values()))
        self.enroll_button.setEnabled(not busy and bool(self._included_paths()))
        self.enrolled_button.setVisible(self.person_stack.currentIndex() == EXISTING)
        for tile in self.tiles.values():
            tile.retry.setEnabled(not busy and self.source is not None)

    def _included_paths(self):
        return [tile.path for tile in self.tiles.values() if tile.path and tile.include.isChecked()]

    def _clear_results(self):
        self.output_dir = None
        for tile in self.tiles.values():
            tile.reset()
        self.progress.hide()

    def _refresh_provider(self):
        config = config_from_settings(self.settings)
        problem = configuration_problem(config)
        if problem:
            self.provider_label.setText(problem)
            self.privacy_label.setText("Photos are sent to the AI provider you configure. "
                                       "Make sure this person has agreed.")
        else:
            self.provider_label.setText(f"Using {config.info.label}, model {config.image_model}.")
            self.privacy_label.setText(f"The photo is sent to {provider_host(config)}. "
                                       "Make sure this person has agreed.")

    # --- person -----------------------------------------------------------
    def _current_employee(self):
        return self.employee_combo.currentData()

    def _reload_employees(self):
        previous = self._current_employee()
        previous_name = previous["name"] if previous else None
        try:
            rows = self.service.employees()
        except CATALOG_ERRORS as exc:
            logging.warning("Could not read the employee list: %s", exc)
            rows = []
        self.employee_combo.blockSignals(True)
        self.employee_combo.clear()
        selected = -1
        for index, row in enumerate(rows):
            status = "" if row.get("active", True) else " (inactive)"
            self.employee_combo.addItem(f"{row['employee_name']} ({row['employee_id']}){status}", row)
            if row["name"] == previous_name:
                selected = index
        self.employee_combo.setCurrentIndex(selected)
        self.employee_combo.blockSignals(False)
        if previous_name and selected < 0:
            self._employee_changed()

    def _mode_changed(self, mode):
        self.person_stack.setCurrentIndex(mode)
        self._fit_person_stack()
        self._clear_results()
        if mode == EXISTING:
            self._employee_changed()
        else:
            self._set_source(None)
        self._update_buttons()

    def _employee_changed(self, *_):
        self._clear_results()
        self._set_source(None)
        if self._current_employee():
            self._use_enrolled_photo()
        self._update_buttons()

    def _person_label(self):
        """The enrollment label for the chosen person, or "" when none is chosen."""
        if self.person_stack.currentIndex() == EXISTING:
            row = self._current_employee()
            return row["name"] if row else ""
        return self.names.values()[2]

    # --- photo ------------------------------------------------------------
    def _set_source(self, image):
        self.source = image
        self._show_source()

    def _show_source(self):
        if self.source is None:
            self.preview.clear()
            self.preview.setText("Drop a photo here\nor choose Upload photo")
            return
        self.preview.setPixmap(to_pixmap(self.source).scaled(
            *PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _enrolled_photo_path(self, row):
        try:
            for photo in sample_photo_map(self.settings.encodings_path).get(row["name"], []):
                if photo is not None and photo.is_file():
                    return photo
        except CATALOG_ERRORS as exc:
            logging.warning("Could not read enrolled photos: %s", exc)
        labels = [self.employee_combo.itemData(i)["name"] for i in range(self.employee_combo.count())]
        return portrait_path(self.settings.encodings_path, row["name"], labels)

    def _use_enrolled_photo(self):
        row = self._current_employee()
        if not row:
            self.toast.show_message("Choose an employee first.", "warn")
            return
        path = self._enrolled_photo_path(row)
        image = read_image(path) if path else None
        if image is None:
            self.toast.show_message(f"No enrolled photo found for {row['employee_name']}. Upload one instead.", "warn")
            return
        self._clear_results()
        self._set_source(image)
        self._update_buttons()

    def _choose_photo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a photo", str(Path.home()), PHOTO_FILTER)
        if path:
            self._load_photo(path)

    def _load_photo(self, path):
        image = read_image(path)
        if image is None:
            self.toast.show_message(f"Could not read {path}", "bad")
            return
        self._clear_results()
        self._set_source(image)
        self._update_buttons()

    # --- generation -------------------------------------------------------
    def _ready_to_generate(self):
        problem = configuration_problem(config_from_settings(self.settings))
        if problem:
            self.toast.show_message(problem, "warn")
        elif not self._person_label():
            problem = ("Choose an employee first." if self.person_stack.currentIndex() == EXISTING
                       else "Enter the employee's full name first.")
            self.toast.show_message(problem, "warn")
        elif self.source is None:
            problem = "Add a photo first."
            self.toast.show_message(problem, "warn")
        return not problem

    def _generate_all(self):
        if self.busy or not self._ready_to_generate():
            return
        self.output_dir = None  # Every full run keeps its own folder; earlier images stay on disk.
        for tile in self.tiles.values():
            tile.reset()
        self._start(ANGLES)

    def _regenerate(self, key):
        if self.busy or not self._ready_to_generate():
            return
        self._start([tile.angle for tile in self.tiles.values() if tile.angle.key == key])

    def _start(self, angles):
        try:
            source_jpeg = encode_source(self.source)
        except (AIError, cv2.error) as exc:
            self.toast.show_message(str(exc), "bad")
            return
        if self.output_dir is None:
            self.output_dir = angles_directory(self.settings.encodings_path, self._person_label())
        self._done = 0
        self.progress.setRange(0, len(angles))
        self.progress.setValue(0)
        self.progress.show()
        worker = AngleWorker(config_from_settings(self.settings), source_jpeg, angles, self.output_dir)
        worker.angleStarted.connect(self._on_angle_started)
        worker.angleDone.connect(self._on_angle_done)
        worker.angleFailed.connect(self._on_angle_failed)
        worker.runDone.connect(self._on_run_done)
        self._worker = worker
        self.toast.show_message("Generating… this can take a minute per image.", "info")
        self._update_buttons()
        start_detached(worker)

    def _stop(self):
        if self._worker is not None:
            self._worker.stop()
            self.stop_button.setEnabled(False)
            self.toast.show_message("Stopping after the current image…", "info")

    def _on_angle_started(self, key):
        self.tiles[key].set_working()

    def _advance(self):
        self._done += 1
        self.progress.setValue(self._done)

    def _on_angle_done(self, key, path):
        self.tiles[key].set_result(path)
        self._advance()
        self._update_buttons()

    def _on_angle_failed(self, key, message):
        self.tiles[key].set_failed(message)
        self._advance()
        self.toast.show_message(message, "bad")

    def _on_run_done(self, stopped):
        self._worker = None
        self.stop_button.setEnabled(True)
        self.progress.hide()
        ready = sum(1 for tile in self.tiles.values() if tile.path)
        for tile in self.tiles.values():
            if tile.status.text() == "Generating…":
                tile.reset()
        if ready == len(self.tiles):
            self.toast.show_message("All six angles are ready and saved. Open folder shows them.", "ok")
        elif not stopped or ready:
            self.toast.show_message(f"{ready} of {len(self.tiles)} angles are ready. "
                                    "Use Redo on any you want again.", "warn" if ready else "bad")
        self._update_buttons()

    def _open_folder(self):
        if self.output_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))

    # --- enrollment -------------------------------------------------------
    def _add_to_enrollment(self):
        if self.busy or self._saving or not guard(self, "manage_employees", "add images to enrollment"):
            return
        paths = self._included_paths()
        existing = self.person_stack.currentIndex() == EXISTING
        row = self._current_employee() if existing else None
        first_name, last_name, full_name = self.names.values()
        employee_id = row["employee_id"] if row else self.id_edit.text().strip()
        name = row["name"] if row else full_name
        if not paths or not name:
            self.toast.show_message("Choose the person and at least one image first.", "warn")
            return
        if not existing and self._matches_existing(name, employee_id):
            self.toast.show_message("This employee already exists. Choose Existing employee instead.", "warn")
            return
        confirmed = QMessageBox.warning(
            self, "Add AI images to enrollment?",
            f"Add {len(paths)} AI-generated image(s) to {name}?\n\n"
            "These aren't real photos. They can make recognition less reliable and may help someone "
            "else match this employee. Each image must still pass the face and identity checks.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        images = [image for image in (read_image(path) for path in paths) if image is not None]
        # A new employee starts from their real photo; AI images are checked against it.
        if not existing:
            images.insert(0, self.source)
        self._saving = True
        self.toast.show_message("Checking and encoding images…", "info")
        self._update_buttons()
        self.runner.submit(images, name, employee_id, True,
                           *(("", "") if existing else (first_name, last_name)))

    def _matches_existing(self, name, employee_id):
        for index in range(self.employee_combo.count()):
            row = self.employee_combo.itemData(index)
            if row["name"].casefold() == name.casefold() or (employee_id and row["employee_id"] == employee_id):
                return True
        return False

    def _on_enrolled(self, outcome):
        self._saving = False
        if outcome.ok:
            self._reload_employees()
            self.engine.reload_catalog()
            self.toast.show_message(f"{outcome.message}. Total samples: {outcome.total_samples}", "ok")
        else:
            self.toast.show_message(outcome.message, "bad")
        self._update_buttons()

    # --- screen hooks -----------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self._reload_employees()
        self._refresh_provider()

    def on_settings_changed(self, settings):
        self.settings = settings
        self._make_service()
        self._refresh_provider()

    def set_theme(self, theme):
        self._theme = theme
        self.toast.set_theme(theme)
        for card in self._cards:
            card.set_theme(theme)
        self._apply_icons()

    def _apply_icons(self):
        colors = palette(self._theme)
        apply_button_icon(self.generate_button, "bolt", "#FFFFFF")
        apply_button_icon(self.upload_button, "upload", colors["muted"])
        apply_button_icon(self.folder_button, "folder", colors["muted"])
        apply_button_icon(self.enroll_button, "user-plus", colors["muted"])

    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.stop()  # Parked by start_detached; it ends after the current request.
        super().closeEvent(event)
