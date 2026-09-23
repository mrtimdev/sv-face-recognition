"""Employees: enrollment by live capture or by photo upload, plus catalog edits.

Encoding is CPU work, so it runs on a short-lived worker thread and reports
back through a Qt signal. The catalog is reloaded into the engine afterwards,
which is why enrollment pauses recording and restarts the engine on save.
"""
import hashlib
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
from PyQt6.QtCore import QObject, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QComboBox, QDialog,
                             QHeaderView, QInputDialog, QTableWidget, QTableWidgetItem, QAbstractItemView,
                             QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget,
                             QVBoxLayout, QWidget)

from ...camera import CameraManager
from ...enrollment import EnrollmentOutcome, EnrollmentService, FACE_BACKEND_LOCK, _atomic_write
from ..bridge import to_pixmap
from ..icons import apply_button_icon
from ..theme import palette
from ..widgets import Avatar, Card, PageHeader, StatCard, StatusPill, ToastBar, VideoView


PHOTO_FILTER = "Images (*.jpg *.jpeg *.png *.bmp *.tiff)"
SAMPLE_LIMIT = 8
AUTO_CAPTURE_COOLDOWN = 1.5
PREVIEW_INTERVAL_MS = 33
THUMB_SIZE = 56
PROFILE_PHOTO_SIZE = 130
GALLERY_THUMB_SIZE = 80
MAX_GALLERY_ITEMS = 12
ENROLLMENT_PHOTOS_DIR = "enrollment_photos"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".tiff")


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _safe_name(name):
    """Reproduce the filename sanitization from storage.py."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))[:60]


def _rounded_pixmap(pixmap, size, radius=10):
    """Create a rounded-corner version of *pixmap* at *size* x *size*."""
    if pixmap is None or pixmap.isNull():
        return QPixmap()
    scaled = pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    painter.setClipPath(path)
    x = (size - scaled.width()) // 2
    y = (size - scaled.height()) // 2
    painter.drawPixmap(x, y, scaled)
    painter.end()
    return result


def _placeholder_pixmap(size, text="?", theme="dark"):
    """A colored circle with an initial when no capture exists."""
    colors = palette(theme)
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size * 0.18, size * 0.18)
    painter.setClipPath(path)
    painter.fillRect(0, 0, size, size, QColor(colors["panel_alt"]))
    painter.setPen(QColor(colors["muted"]))
    font = QFont()
    font.setPixelSize(max(10, size // 3))
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, text[:2].upper())
    painter.end()
    return result


# ---------------------------------------------------------------------------
#  Worker threads
# ---------------------------------------------------------------------------

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
        except Exception as exc:
            outcome = EnrollmentOutcome("error", str(exc), name=name, employee_id=employee_id or "")
        self.finished.emit(outcome)


class SampleRunner(QObject):
    """Validates one prospective sample off the Qt thread."""

    validated = pyqtSignal(object, object)

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
        except Exception as exc:
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


# ---------------------------------------------------------------------------
#  Main screen
# ---------------------------------------------------------------------------

class EmployeeDialog(QDialog):
    def reject(self):
        owner = self.parent()
        if owner.saving or owner.validating or owner.probing:
            owner.editor_toast.show_message("Please wait for the current photo check to finish.", "info")
            return
        super().reject()


class EmployeesScreen(QWidget):
    probeReady = pyqtSignal(object)

    def __init__(self, engine, settings, parent=None, preview_capture_factory=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._preview_factory = preview_capture_factory
        self.service = EnrollmentService(settings.encodings_path, settings.employees_path)
        self.runner = EnrollmentRunner(self.service, self)
        self.pending = []
        self.saving = False
        self._editing_label = None
        self.upload = None
        self.rows = []
        self.countdown = 0
        self.probing = False
        self.validating = False
        self._sample_source = "live"
        self.sample_runner = SampleRunner(self.service, self)
        self.last_auto_capture = 0.0
        self.preview_on = False
        self.preview_camera = None
        self._engine_was_running = False
        self._thumb_cache = {}
        self._captures_map = {}
        self._captures_dirty = True
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self._build()
        self._connect()
        self.reload()

    # =======================================================================
    #  Layout
    # =======================================================================

    def _notify(self, text, tone="info"):
        self.toast.show_message(text, tone)
        if self.editor.isVisible():
            self.editor_toast.show_message(text, tone)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(16)
        self.header = PageHeader("Enrolled Employees", "Manage people, enrollment photos and attendance access.")
        self.header.layout().setStretch(0, 1)
        self.enrolled_pill = StatusPill("0 enrolled", "idle", dot=True)
        self.reload_button = QPushButton("Refresh")
        self.folder_button = QPushButton("Open data folder")
        self.folder_button.hide()
        self.enroll_cta = QPushButton("Add employee")
        self.enroll_cta.setObjectName("primary")
        self.header.add_action(self.reload_button)
        self.header.add_action(self.enroll_cta)
        root.addWidget(self.header)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.stat_enrolled = StatCard("Ready for attendance", "0", hint="Employees with enrolled faces", icon="users", tone="green")
        self.stat_latest = StatCard("Needs enrollment", "0", hint="Add a face photo to get started", icon="user-plus", tone="orange")
        self.stat_samples = StatCard("Face samples", "0", hint="Face samples available for matching", icon="camera", tone="blue")
        self.stat_captures = StatCard("Evidence captures", "0", parent=self)
        self.stat_captures.hide()
        for card in (self.stat_enrolled, self.stat_latest, self.stat_samples):
            cards.addWidget(card)
        root.addLayout(cards)
        root.addWidget(self._build_directory(), 1)
        self.toast = ToastBar(theme=self._theme)
        root.addWidget(self.toast)

        # Capture tools appear only when requested, leaving the directory usable.
        self.editor = EmployeeDialog(self)
        self.editor.setWindowModality(Qt.WindowModality.WindowModal)
        self.editor.resize(720, 780)
        editor_layout = QVBoxLayout(self.editor)
        editor_scroll = QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self._build_wizard())
        self.right_stack.addWidget(self._build_profile_card())
        editor_scroll.setWidget(self.right_stack)
        editor_layout.addWidget(editor_scroll)
        self.editor_toast = ToastBar(theme=self._theme)
        editor_layout.addWidget(self.editor_toast)
        close = QPushButton("Close")
        close.clicked.connect(self.editor.reject)
        actions = QHBoxLayout()
        actions.addWidget(self.clear_button)
        actions.addStretch()
        actions.addWidget(close)
        actions.addWidget(self.save_button)
        editor_layout.addLayout(actions)
        self.right_stack.currentChanged.connect(lambda index: self.save_button.setVisible(index == 0))
        self.right_stack.currentChanged.connect(lambda index: self.clear_button.setVisible(index == 0))
        self.editor.finished.connect(self._editor_closed)

    def _build_directory(self):
        card = Card("Employee directory", "", icon="users", theme=self._theme)
        self.directory_card = card
        tools = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name or employee ID")
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All employees", "Ready for attendance", "Needs enrollment"])
        self.status_filter.setMinimumWidth(180)
        tools.addWidget(self.search_edit, 1)
        tools.addWidget(self.status_filter)
        card.add_layout(tools)
        self.employee_table = QTableWidget(0, 6)
        self.employee_table.setHorizontalHeaderLabels(["PHOTO", "EMPLOYEE", "EMPLOYEE ID", "STATUS", "SAMPLES", "ACTIONS"])
        self.employee_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.employee_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.employee_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.employee_table.setShowGrid(False)
        self.employee_table.setWordWrap(False)
        self.employee_table.setAlternatingRowColors(True)
        self.employee_table.verticalHeader().hide()
        self.employee_table.setMinimumHeight(220)
        self.employee_table.setIconSize(QSize(48, 48))
        header = self.employee_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        for col, width in ((0, 80), (2, 170), (3, 160), (4, 85), (5, 275)):
            self.employee_table.setColumnWidth(col, width)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(80)
        card.add(self.employee_table, 1)
        self.empty_label = QLabel("No employees yet. Click Add employee to enroll your first person.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("screenSubtitle")
        card.add(self.empty_label)
        self.result_count = QLabel()
        self.result_count.setObjectName("screenSubtitle")
        card.add(self.result_count)
        return card

    def _build_wizard(self):
        card = Card("Add employee", "Enter their details, then capture or upload a clear face photo.", icon="user-plus", theme=self._theme)
        self.wizard_card = card

        form = QGridLayout()
        form.setSpacing(8)
        form.addWidget(QLabel("Display name"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Required, e.g. Jane Smith")
        form.addWidget(self.name_edit, 0, 1)
        form.addWidget(QLabel("Employee ID"), 1, 0)
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("Optional, e.g. EMP-125")
        form.addWidget(self.id_edit, 1, 1)
        self.quality_check = QCheckBox("Reject low-quality samples")
        self.quality_check.setChecked(True)
        form.addWidget(self.quality_check, 2, 0, 1, 2)
        card.add_layout(form)

        # Segmented control for capture / upload
        seg = QHBoxLayout()
        seg.setSpacing(4)
        self.seg_capture = QPushButton("Live Capture")
        self.seg_upload = QPushButton("Upload Photo")
        seg.addWidget(self.seg_capture)
        seg.addWidget(self.seg_upload)
        seg.addStretch(1)
        card.add_layout(seg)

        self.capture_stack = QStackedWidget()
        self.capture_stack.addWidget(self._build_capture_content())  # 0
        self.capture_stack.addWidget(self._build_upload_content())   # 1
        card.add(self.capture_stack, 1)

        sample_heading = QLabel("Collected samples")
        sample_heading.setObjectName("sectionTitle")
        card.add(sample_heading)

        self.samples_list = QListWidget()
        self.samples_list.setFixedHeight(80)
        card.add(self.samples_list)

        self.clear_button = QPushButton("Clear samples")
        self.save_button = QPushButton("Save enrollment")
        self.save_button.setObjectName("primary")

        self.hint = QLabel("Use a clear photo with only this employee in view. "
                           "The first photo will appear in the employee directory.")
        self.hint.setObjectName("screenSubtitle")
        self.hint.setWordWrap(True)
        card.add(self.hint)

        return card

    def _build_capture_content(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)

        self.capture_video = VideoView(widget, message="Preview off",
                                       subtitle="Start the preview to capture samples")
        self.capture_video.setMinimumHeight(200)
        frame = QFrame()
        frame.setObjectName("cameraFrame")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        frame_layout.addWidget(self.capture_video)
        layout.addWidget(frame, 1)

        self.coach = QLabel("Type the display name, start the preview, "
                            "then capture samples.")
        self.coach.setObjectName("screenSubtitle")
        self.coach.setWordWrap(True)
        layout.addWidget(self.coach)

        row = QHBoxLayout()
        self.preview_button = QPushButton("Start preview")
        self.capture_button = QPushButton("Capture sample")
        self.capture_button.setObjectName("primary")
        self.capture_button.setEnabled(False)
        self.auto_check = QCheckBox("Auto")
        self.auto_check.setToolTip(
            "Capture automatically while exactly one readable face is in view")
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
        return widget

    def _build_upload_content(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 4, 0, 0)
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
        return widget

    # -----------------------------------------------------------------------
    #  Employee profile card (right column, index 1)
    # -----------------------------------------------------------------------

    def _build_profile_card(self):
        card = Card("Employee Profile", "", icon="user", theme=self._theme)
        self.profile_card_widget = card

        header = QHBoxLayout()
        header.setSpacing(16)
        self.profile_photo = QLabel()
        self.profile_photo.setFixedSize(PROFILE_PHOTO_SIZE, PROFILE_PHOTO_SIZE)
        self.profile_photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.profile_photo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.profile_photo.mousePressEvent = self._profile_photo_clicked
        header.addWidget(self.profile_photo)

        info = QVBoxLayout()
        info.setSpacing(4)
        self.profile_name = QLabel("")
        font = self.profile_name.font()
        font.setPointSize(16)
        font.setWeight(QFont.Weight.Bold)
        self.profile_name.setFont(font)
        self.profile_id_label = QLabel("")
        self.profile_id_label.setObjectName("sectionTitle")
        self.profile_pill = StatusPill("", "idle")
        self.profile_info = QLabel("")
        self.profile_info.setObjectName("screenSubtitle")
        self.profile_info.setWordWrap(True)
        info.addWidget(self.profile_name)
        info.addWidget(self.profile_id_label)
        info.addWidget(self.profile_pill)
        info.addWidget(self.profile_info)
        info.addStretch()
        header.addLayout(info, 1)
        card.add_layout(header)

        gallery_header = QHBoxLayout()
        gallery_title = QLabel("Evidence captures")
        gallery_title.setObjectName("sectionTitle")
        self.gallery_count_label = QLabel("")
        self.gallery_count_label.setObjectName("screenSubtitle")
        gallery_header.addWidget(gallery_title)
        gallery_header.addStretch()
        gallery_header.addWidget(self.gallery_count_label)
        card.add_layout(gallery_header)

        self.gallery_scroll = QScrollArea()
        self.gallery_scroll.setWidgetResizable(True)
        self.gallery_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.gallery_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.gallery_scroll.setFixedHeight(GALLERY_THUMB_SIZE + 20)
        self.gallery_scroll.setObjectName("panel")
        self.gallery_container = QWidget()
        self.gallery_layout = QHBoxLayout(self.gallery_container)
        self.gallery_layout.setContentsMargins(4, 4, 4, 4)
        self.gallery_layout.setSpacing(8)
        self.gallery_layout.addStretch()
        self.gallery_scroll.setWidget(self.gallery_container)
        card.add(self.gallery_scroll)

        self.gallery_hint = QLabel("Click any photo to view full size")
        self.gallery_hint.setObjectName("screenSubtitle")
        card.add(self.gallery_hint)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.p_add_btn = QPushButton("Add samples")
        self.p_del_btn = QPushButton("Remove latest sample")
        self.p_rem_btn = QPushButton("Remove employee")
        self.p_rem_btn.setObjectName("danger")
        for btn in (self.p_add_btn, self.p_del_btn, self.p_rem_btn):
            actions.addWidget(btn)
        actions.addStretch()
        card.add_layout(actions)

        row = QHBoxLayout()
        self.p_open_folder_btn = QPushButton("Open captures")
        self.p_enroll_btn = QPushButton("Enroll new")
        self.p_enroll_btn.setObjectName("primary")
        row.addWidget(self.p_open_folder_btn)
        row.addStretch()
        row.addWidget(self.p_enroll_btn)
        card.add_layout(row)

        return card

    # =======================================================================
    #  Wiring
    # =======================================================================

    def _connect(self):
        self.engine.catalogChanged.connect(lambda rows: self.reload())
        self.engine.attendanceSaved.connect(lambda _: self._invalidate_captures())
        self.runner.finished.connect(self.on_enrollment_finished)
        self.sample_runner.validated.connect(self.on_sample_validated)
        self.probeReady.connect(self.on_probe)

        # Timers
        self.preview_timer = QTimer(self)
        self.preview_timer.setInterval(PREVIEW_INTERVAL_MS)
        self.preview_timer.timeout.connect(self._pull_preview)
        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._tick_countdown)
        self.probe_timer = QTimer(self)
        self.probe_timer.setInterval(700)
        self.probe_timer.timeout.connect(self._probe_now)

        # UI actions
        self.preview_button.clicked.connect(self._toggle_preview)
        self.capture_button.clicked.connect(self._start_countdown)
        self.clear_button.clicked.connect(self._clear_samples)
        self.save_button.clicked.connect(self._save)
        self.choose_button.clicked.connect(self._choose_photo)
        self.drop.dropped.connect(self._load_photo)
        self.add_upload_button.clicked.connect(self._add_upload_sample)
        self.reload_button.clicked.connect(self.reload)
        self.folder_button.clicked.connect(self._open_folder)
        self.enroll_cta.clicked.connect(self._switch_to_wizard)

        # Segment control
        self.seg_capture.clicked.connect(lambda: self._switch_segment(0))
        self.seg_upload.clicked.connect(lambda: self._switch_segment(1))

        # Search
        self.search_edit.textChanged.connect(self._filter_employees)
        self.status_filter.currentIndexChanged.connect(lambda _: self._filter_employees(self.search_edit.text()))

        # Employee list
        self.employee_table.cellDoubleClicked.connect(lambda *_: self._show_profile())

        # Profile card actions
        self.p_add_btn.clicked.connect(self._use_selected)
        self.p_del_btn.clicked.connect(self._remove_sample)
        self.p_rem_btn.clicked.connect(self._remove_employee)
        self.p_enroll_btn.clicked.connect(self._switch_to_wizard)
        self.p_open_folder_btn.clicked.connect(self._open_captures_for_selected)

        # Initial state
        self._switch_segment(0)
        self._apply_icons()

    def _tab_changed(self, index):
        capture = index == 0
        if capture and self.preview_on:
            self.probe_timer.start()
        else:
            self.probe_timer.stop()
        if not capture and self.preview_on:
            self._toggle_preview()

    def _switch_segment(self, index):
        self.capture_stack.setCurrentIndex(index)
        if index == 0:
            self.seg_capture.setObjectName("softButton")
            self.seg_upload.setObjectName("")
        else:
            self.seg_capture.setObjectName("")
            self.seg_upload.setObjectName("softButton")
        for btn in (self.seg_capture, self.seg_upload):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._tab_changed(index)

    def _filter_employees(self, text):
        text = text.casefold().strip()
        shown = 0
        for index, row in enumerate(self.rows):
            matches = not text or any(text in str(row[key]).casefold()
                                     for key in ("employee_name", "employee_id", "name"))
            status = self.status_filter.currentIndex()
            visible = matches and (status == 0 or (status == 1 and row["samples"] > 0)
                                   or (status == 2 and row["samples"] == 0))
            self.employee_table.setRowHidden(index, not visible)
            shown += visible
        self.result_count.setText(f"Showing {shown} of {len(self.rows)} enrollment records • Double-click a row to view details")
        self.empty_label.setText("No employees match your search." if self.rows else
                                 "No employees yet. Click Add employee to enroll your first person.")
        self.empty_label.setVisible(not shown)

    def _apply_icons(self):
        colors = palette(self._theme)
        apply_button_icon(self.reload_button, "refresh", colors["muted"])
        apply_button_icon(self.folder_button, "folder", colors["muted"])
        apply_button_icon(self.enroll_cta, "user-plus", "#FFFFFF")

    # =======================================================================
    #  Photo management
    # =======================================================================

    # --- enrollment photo directory ----------------------------------------

    def _enrollment_photo_dir(self):
        return Path(self.settings.encodings_path).parent / ENROLLMENT_PHOTOS_DIR

    def _enrollment_photo_path(self, label):
        """Canonical path for a saved enrollment photo by enrollment label."""
        digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
        return self._enrollment_photo_dir() / f"{_safe_name(label)}_{digest}.jpg"

    def _save_enrollment_photo(self, label, samples):
        """Keep the directory photo tied to this exact enrollment label."""
        if not samples:
            return False
        try:
            ok, encoded = cv2.imencode(".jpg", samples[0], [cv2.IMWRITE_JPEG_QUALITY, 92])
            if not ok:
                return False
            _atomic_write(self._enrollment_photo_path(label), lambda handle: handle.write(encoded.tobytes()))
            return True
        except (OSError, cv2.error):
            return False

    def _find_enrollment_photo(self, label, display_name):
        path = self._enrollment_photo_path(label)
        if path.is_file():
            return path
        # Old installations saved sanitized filenames. Never guess between collisions.
        legacy = self._enrollment_photo_dir() / f"{_safe_name(label)}.jpg"
        collisions = sum(_safe_name(row["name"]).casefold() == _safe_name(label).casefold()
                         for row in self.rows)
        return legacy if legacy.is_file() and collisions <= 1 else None

    # --- evidence captures (for gallery) ----------------------------------

    def _scan_captures(self):
        """Build {safe_name: [Path, ...]} sorted newest-first for every employee."""
        capture_dir = Path(self.settings.capture_dir)
        result = {}
        if not capture_dir.is_dir():
            return result
        try:
            files = sorted(capture_dir.glob("*.jpg"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return result
        for path in files:
            stem = path.stem
            # UUID is exactly 36 chars; filename is {safe}_{uuid}.jpg
            if len(stem) > 37:
                prefix = stem[:-37]
            else:
                prefix = stem
            result.setdefault(prefix, []).append(path)
        return result

    # --- thumbnail cache ---------------------------------------------------

    def _thumbnail_for(self, path, size=THUMB_SIZE):
        """Load an image from disk, scale it and cache the QPixmap."""
        key = (str(path), size)
        cached = self._thumb_cache.get(key)
        if cached is not None:
            return cached
        try:
            pixmap = QPixmap(str(path))
        except Exception:
            return None
        if pixmap.isNull():
            return None
        rounded = _rounded_pixmap(pixmap, size)
        self._thumb_cache[key] = rounded
        return rounded

    def _open_image(self, path):
        """Open an image in the system viewer."""
        path = Path(path)
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self._notify("Image file not found on disk.", "warn")

    # =======================================================================
    #  Profile card population
    # =======================================================================

    def _switch_to_wizard(self):
        self._open_enrollment()

    def _open_enrollment(self, row=None):
        if self.saving or self.validating:
            return
        self._clear_samples()
        self.editor_toast.hide_message()
        self.editor_toast.hide_message()
        self._editing_label = row["name"] if row else None
        self.name_edit.setText(row["employee_name"] if row else "")
        self.id_edit.setText(row["employee_id"] if row else "")
        self.name_edit.setReadOnly(row is not None)
        self.id_edit.setReadOnly(row is not None)
        title = "Update employee photo" if row else "Add employee"
        self.wizard_card.title_label.setText(title)
        self.wizard_card.header.layout().setStretch(1, 1)
        self.editor.setWindowTitle(title)
        self.wizard_card.set_subtitle("New face samples improve matching. The first photo becomes the directory image."
                                     if row else "Enter their details, then capture or upload a clear face photo.")
        self.save_button.setText("Save photos" if row else "Add employee")
        self.right_stack.setCurrentIndex(0)
        self.editor.show()
        self.coach.setText("Start the preview to capture a face, or choose Upload Photo.")
        self.name_edit.setFocus()

    def _editor_closed(self, _result):
        self.countdown_timer.stop()
        self.countdown = 0
        self.capture_button.setText("Capture sample")
        if self.preview_on:
            self._stop_preview()
        self._clear_samples()
        self._editing_label = None

    def _switch_to_profile(self):
        self.right_stack.setCurrentIndex(1)

    def _show_profile(self):
        row = self.selected_row()
        if row:
            self._populate_profile(row)
            self._switch_to_profile()
            self.editor.setWindowTitle("Employee details")
            self.editor.show()

    def _populate_profile(self, row):
        """Fill the profile card from a row dict, enrollment photo and captures."""
        if row is None:
            return

        # -- enrollment photo (main profile image) --
        enrollment_photo = self._find_enrollment_photo(
            row["name"], row["employee_name"])
        if enrollment_photo:
            photo = self._thumbnail_for(enrollment_photo, PROFILE_PHOTO_SIZE)
            if photo and not photo.isNull():
                self.profile_photo.setPixmap(photo)
            else:
                self.profile_photo.setPixmap(
                    _placeholder_pixmap(PROFILE_PHOTO_SIZE, row["employee_name"], self._theme))
            self._profile_photo_path = str(enrollment_photo)
        else:
            self.profile_photo.setPixmap(
                _placeholder_pixmap(PROFILE_PHOTO_SIZE, row["employee_name"], self._theme))
            self._profile_photo_path = ""

        # -- text info --
        safe = _safe_name(row["employee_name"])
        captures = self._captures_map.get(safe, [])
        self.profile_name.setText(row["employee_name"])
        self.profile_id_label.setText(
            f"ID: {row['employee_id']}" if row["employee_id"] else "No employee ID")
        self.profile_pill.set_status(
            "Enrolled" if row["samples"] else "Needs enrollment",
            "ok" if row["samples"] else "warn")
        lines = [f"{row['samples']} enrolled sample(s)"]
        if enrollment_photo:
            lines.append(f"Enrollment photo: {enrollment_photo.name}")
        else:
            lines.append("No enrollment photo (re-enroll to save one)")
        if captures:
            lines.append(f"{len(captures)} attendance capture(s)")
            try:
                mtime = captures[0].stat().st_mtime
                lines.append(
                    f"Last seen: {datetime.fromtimestamp(mtime).strftime('%d %b %Y  %H:%M')}")
            except OSError:
                pass
        self.profile_info.setText("\n".join(lines))

        # -- enable/disable actions --
        self.p_del_btn.setEnabled(row["samples"] > 0)

        # -- gallery shows evidence captures --
        self._populate_gallery(captures)

    def _populate_gallery(self, captures):
        """Fill the scrollable gallery with clickable thumbnails."""
        # Clear existing thumbnails
        while self.gallery_layout.count():
            child = self.gallery_layout.takeAt(0)
            widget = child.widget()
            if widget:
                widget.deleteLater()

        shown = captures[:MAX_GALLERY_ITEMS]
        self.gallery_count_label.setText(
            f"{len(captures)} total" if captures else "none")

        if not shown:
            self.gallery_hint.setText("No captures available for this employee")
            self.gallery_layout.addStretch()
            return

        for path in shown:
            thumb = self._thumbnail_for(path, GALLERY_THUMB_SIZE)
            label = QLabel()
            label.setFixedSize(GALLERY_THUMB_SIZE, GALLERY_THUMB_SIZE)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setCursor(Qt.CursorShape.PointingHandCursor)
            label.setToolTip(f"{path.name}\nClick to view full size")
            if thumb and not thumb.isNull():
                label.setPixmap(thumb)
            else:
                label.setPixmap(
                    _placeholder_pixmap(GALLERY_THUMB_SIZE, "?", self._theme))
            # Closure to capture `path`
            label.mousePressEvent = (lambda p: lambda e: self._open_image(p))(path)
            self.gallery_layout.addWidget(label)

        self.gallery_layout.addStretch()

        remaining = len(captures) - len(shown)
        if remaining > 0:
            self.gallery_hint.setText(
                f"Showing {len(shown)} of {len(captures)} captures. "
                "Click any photo to view full size.")
        else:
            self.gallery_hint.setText("Click any photo to view full size")

    def _profile_photo_clicked(self, event):
        path = getattr(self, "_profile_photo_path", "")
        if path:
            self._open_image(path)

    def _open_captures_for_selected(self):
        row = self.selected_row()
        if row is None:
            self._open_folder()
            return
        capture_dir = Path(self.settings.capture_dir)
        capture_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(capture_dir)))

    # =======================================================================
    #  Live capture (dedicated camera, independent of the engine)
    # =======================================================================

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
            self._notify(f"Camera settings are invalid: {exc}", "bad")
            return
        self._engine_was_running = self.engine.running
        if self._engine_was_running:
            try:
                self.engine.stop()
            except Exception as exc:
                self._engine_was_running = False
                self._notify(f"Could not pause attendance: {exc}", "bad")
                return
            self._notify("Attendance paused while the enrollment camera is open; "
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
                self._notify("Attendance resumed.", "ok")
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                self._notify(f"Preview closed, but attendance could not "
                                        f"restart: {exc}", "warn")
        else:
            self._engine_was_running = False

    def _probe_now(self):
        if (self.saving or self.probing or self.validating or not self.preview_on
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
            with FACE_BACKEND_LOCK:
                boxes = self.service._detector().face_locations(
                    cv2.cvtColor(small, cv2.COLOR_BGR2RGB), model="yunet")
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
        if self.saving or not self.preview_on:
            return
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
        if not self.preview_on or self.saving or not self.auto_check.isChecked() or self.validating:
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
        if self.saving or self.validating or not self.preview_on:
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
            self._notify(text, "warn")
            return
        self.pending.append(frame)
        self._refresh_samples()
        if upload_mode:
            self.upload = None
            self.drop.clear()
            self.drop.setText("Drop a photo here\nor choose one below")
            self.add_upload_button.setEnabled(False)
            self.coach.setText(f"Photo added as sample {len(self.pending)}. "
                               "Choose another photo or switch to Live capture.")
            self._notify(f"Photo added as sample {len(self.pending)}", "ok")
        else:
            self.coach.setText(f"Sample {len(self.pending)} added. Vary the angle and capture more, "
                               "then save.")
            self._notify(f"Sample {len(self.pending)} captured", "ok")

    # --- upload -----------------------------------------------------------
    def _choose_photo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a photo", str(Path.home()), PHOTO_FILTER)
        if path:
            self._load_photo(path)

    def _load_photo(self, path):
        image = cv2.imread(path)
        if image is None:
            self._notify(f"Could not read {path}", "bad")
            return
        self.upload = image
        pixmap = to_pixmap(image)
        self.drop.setPixmap(pixmap.scaled(self.drop.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        self.add_upload_button.setEnabled(True)
        self.drop.setToolTip(path)

    def _add_upload_sample(self):
        if self.upload is None:
            self._notify("Choose a photo first.", "warn")
            return
        if self.validating:
            self._notify("A sample check is already running.", "warn")
            return
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
        self.save_button.setEnabled(bool(self.pending) and not self.saving)

    def _clear_samples(self):
        self.pending.clear()
        self.upload = None
        self.drop.clear()
        self.drop.setText("Drop a photo here\nor choose one below")
        self.add_upload_button.setEnabled(False)
        self._refresh_samples()
        self.coach.setText("Samples cleared.")

    def _save(self):
        if self.saving or self.validating:
            return
        name = self._editing_label or self.name_edit.text().strip()
        if not name:
            self._notify("Enter the employee display name.", "warn")
            return
        if not self.pending:
            self._notify("Capture or upload at least one sample.", "warn")
            return
        if self._editing_label is None:
            employee_id = self.id_edit.text().strip()
            if any(row["name"].casefold() == name.casefold() or
                   (employee_id and row["employee_id"] == employee_id) for row in self.rows):
                self.editor_toast.show_message("This employee already exists. Use Edit or Update photo in the directory.", "warn")
                return
        self.saving = True
        self.probe_timer.stop()
        self.countdown_timer.stop()
        self.countdown = 0
        self.right_stack.setEnabled(False)
        # Keep a reference so we can save an enrollment photo after success.
        self._enrollment_save_samples = [s.copy() for s in self.pending]
        self.save_button.setEnabled(False)
        self.save_button.setText("Saving...")
        self._notify("Encoding samples...", "info")
        self.runner.submit(self.pending, name, self.id_edit.text().strip(),
                           self.quality_check.isChecked())

    def on_enrollment_finished(self, outcome):
        self.saving = False
        self.right_stack.setEnabled(True)
        self.save_button.setText("Save photos" if self._editing_label else "Add employee")
        self.save_button.setEnabled(bool(self.pending))
        if not outcome.ok:
            self._enrollment_save_samples = []
            self.editor_toast.show_message(f"{outcome.message}", "bad")
            if self.preview_on:
                self.probe_timer.start()
            return
        # Save the first sample as the enrollment profile photo.
        photo_saved = self._save_enrollment_photo(
            outcome.name, getattr(self, "_enrollment_save_samples", []))
        self._enrollment_save_samples = []
        self.pending.clear()
        self._refresh_samples()
        self._notify(f"{outcome.message}. Total samples: {outcome.total_samples}", "ok")
        self.name_edit.clear()
        self.id_edit.clear()
        self.reload()
        self.engine.reload_catalog()
        self.editor.accept()
        if not photo_saved:
            self._notify("Enrollment saved, but the directory photo could not be saved. Check folder permissions and update the photo again.", "warn")

    # =======================================================================
    #  Catalog management (list, selection, delete)
    # =======================================================================

    def _invalidate_captures(self):
        self._captures_dirty = True

    def reload(self):
        if self._captures_dirty:
            self._captures_map = self._scan_captures()
            self._captures_dirty = False
        self._thumb_cache.clear()
        self.rows = self.service.employees()
        self.employee_table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            photo = self._find_enrollment_photo(row["name"], row["employee_name"])
            pixmap = self._thumbnail_for(photo, 48) if photo else None
            if pixmap is None or pixmap.isNull():
                pixmap = _placeholder_pixmap(48, "".join(word[0] for word in row["employee_name"].split()[:2]), self._theme)
            image_item = QTableWidgetItem()
            image_item.setIcon(QIcon(pixmap))
            image_item.setToolTip("Saved enrollment photo" if photo else "No saved enrollment photo")
            self.employee_table.setItem(index, 0, image_item)
            for col, value in ((1, row["employee_name"]), (2, row["employee_id"]), (4, str(row["samples"]))):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.employee_table.setItem(index, col, item)
            status = QWidget()
            status_layout = QHBoxLayout(status)
            status_layout.setContentsMargins(8, 0, 8, 0)
            pill = StatusPill("Enrolled" if row["samples"] else "Needs enrollment",
                              "ok" if row["samples"] else "warn")
            pill.setFixedHeight(28)
            status_layout.addWidget(pill)
            status_layout.addStretch()
            self.employee_table.setCellWidget(index, 3, status)
            status.setObjectName("employeeCell")
            status.setStyleSheet("QWidget#employeeCell { background: transparent; }")
            actions = QWidget()
            actions.setObjectName("employeeCell")
            actions.setStyleSheet("QWidget#employeeCell { background: transparent; }")
            action_layout = QHBoxLayout(actions)
            action_layout.setContentsMargins(4, 8, 4, 8)
            action_layout.setSpacing(6)
            for text, callback, tone in (("Edit", self._edit_selected, ""),
                                         ("Update photo", self._use_selected, "softButton"),
                                         ("Delete", self._remove_employee, "danger")):
                button = QPushButton(text)
                button.setObjectName(tone)
                button.setToolTip(f"{text}: {row['employee_name']}")
                button.clicked.connect(lambda checked=False, i=index, action=callback: self._row_action(i, action))
                action_layout.addWidget(button)
            self.employee_table.setCellWidget(index, 5, actions)
            self.employee_table.setRowHeight(index, 64)
        ready = len({row["employee_id"] for row in self.rows if row["samples"]})
        pending = len({row["employee_id"] for row in self.rows if not row["samples"]}
                      - {row["employee_id"] for row in self.rows if row["samples"]})
        self.stat_enrolled.set_value(ready, "ok" if ready else "idle")
        self.stat_latest.set_value(pending, "warn" if pending else "idle")
        self.stat_samples.set_value(sum(row["samples"] for row in self.rows))
        self.enrolled_pill.set_status(f"{ready} enrolled", "ok" if ready else "idle")
        self._filter_employees(self.search_edit.text())

    def _row_action(self, index, action):
        self.employee_table.selectRow(index)
        action()

    def selected_row(self):
        selected = self.employee_table.selectionModel().selectedRows()
        index = selected[0].row() if selected else -1
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def _edit_selected(self):
        row = self.selected_row()
        if row is None:
            return
        name, accepted = QInputDialog.getText(self, "Edit employee", "Display name\nEmployee ID: " + row["employee_id"],
                                              text=row["employee_name"])
        if not accepted:
            return
        try:
            self.service.update_employee(row["name"], name)
        except (ValueError, OSError) as exc:
            self._notify(str(exc), "bad")
            return
        self.reload()
        self.engine.reload_catalog()
        self._notify("Employee updated. Employee ID and attendance history are unchanged.", "ok")

    def _use_selected(self):
        row = self.selected_row()
        if row:
            self._open_enrollment(row)

    def _remove_sample(self):
        row = self.selected_row()
        if row is None:
            return
        if QMessageBox.question(self, "Remove latest sample",
                                "Remove the most recently added face sample? Attendance history is kept.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = self.service.delete_sample(row["name"], max(0, row["samples"] - 1))
        except (ValueError, OSError) as exc:
            self._notify(str(exc), "bad")
            return
        self.reload()
        self.engine.reload_catalog()
        self.editor.accept()
        self._notify(f"Removed one sample from {row['name']!r} "
                                f"({removed} encoding(s) deleted)", "ok")

    def _remove_employee(self):
        row = self.selected_row()
        if row is None:
            return
        confirmed = QMessageBox.question(
            self, "Delete employee enrollment",
            f"Remove all {row['samples']} enrolled sample(s) for {row['name']!r}?\n\n"
            "Attendance history is kept: existing rows keep their recorded name and ID. "
            "This removes the selected enrollment from the directory.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        photo_paths = {self._enrollment_photo_path(row["name"])}
        legacy = self._enrollment_photo_dir() / f"{_safe_name(row['name'])}.jpg"
        if sum(_safe_name(item["name"]).casefold() == _safe_name(row["name"]).casefold()
               for item in self.rows) == 1:
            photo_paths.add(legacy)
        try:
            removed = self.service.delete_employee(row["name"])
        except (ValueError, OSError) as exc:
            self._notify(str(exc), "bad")
            return
        photo_error = False
        for path in photo_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                photo_error = True
        self.editor.accept()
        self.reload()
        self.engine.reload_catalog()
        self._notify(f"Removed {row['name']!r} ({removed} encoding(s)). "
                                "Attendance history is unchanged.", "ok")
        if photo_error:
            self._notify("Employee enrollment deleted, but a saved photo could not be removed from disk.", "warn")

    def _open_folder(self):
        folder = Path(self.settings.encodings_path).parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # =======================================================================
    #  Settings / lifecycle
    # =======================================================================

    def on_settings_changed(self, settings):
        self.settings = settings
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self.service = EnrollmentService(settings.encodings_path, settings.employees_path)
        self.runner.service = self.service
        self.sample_runner.service = self.service
        if self.preview_on:
            self._stop_preview()
        self.reload()

    def closeEvent(self, event):
        self.editor.hide()
        self.probe_timer.stop()
        self.countdown_timer.stop()
        self.preview_timer.stop()
        if self.preview_on:
            self._stop_preview(resume=False)
        for signal in (self.probeReady, self.runner.finished, self.sample_runner.validated):
            try:
                signal.disconnect()
            except TypeError:
                pass
        super().closeEvent(event)

    def set_theme(self, theme):
        self._theme = theme
        self.capture_video.set_theme(theme)
        self.toast.set_theme(theme)
        for card in (self.stat_enrolled, self.stat_samples,
                     self.stat_captures, self.stat_latest):
            card.set_theme(theme)
        for panel in (self.directory_card, self.wizard_card, self.profile_card_widget):
            panel.set_theme(theme)
        self.enrolled_pill.set_status(
            self.enrolled_pill.label.text(),
            self.enrolled_pill.property("tone") or "idle")
        self.editor_toast.set_theme(theme)
        self._apply_icons()
        self.reload()
        # Refresh profile if visible
        row = self.selected_row()
        if row is not None and self.right_stack.currentIndex() == 1:
            self._populate_profile(row)
