"""Employees: enrollment by live capture or by photo upload, plus catalog edits.

Encoding is CPU work, so it runs on a short-lived worker thread and reports
back through a Qt signal. The catalog is reloaded into the engine afterwards,
which is why enrollment pauses recording and restarts the engine on save.
"""
import logging
import threading
import time
import hashlib
from datetime import datetime
from pathlib import Path

import cv2
from PyQt6.QtCore import QObject, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                             QLabel, QLineEdit, QListWidget, QListWidgetItem, QComboBox, QDialog,
                             QDialogButtonBox, QHeaderView, QMenu, QTableWidget, QTableWidgetItem,
                             QAbstractItemView, QMessageBox, QPushButton, QScrollArea, QSpinBox,
                             QStackedWidget, QVBoxLayout, QWidget)

from ...camera import CameraManager
from ...enrollment import EnrollmentOutcome, EnrollmentService, FACE_BACKEND_LOCK, MAX_ENROLLMENT_SAMPLES, _atomic_write
from ...storage import delete_capture, read_image
from ...enrollment_photos import portrait_name, sample_photo_map
from ...transfer import TransferError, export_employees, import_employees
from ... import angle_checks, angle_templates
from ... import hrm_directory as hd
from ...ai_providers import ANGLES
from ...hrm_sync import HRMError, config_from_settings
from ..access import guard
from ..bridge import to_pixmap
from ..icons import apply_button_icon, make_icon
from ..theme import palette
from ..widgets import Card, PageHeader, StatCard, StatusPill, ToastBar, VideoView


PHOTO_FILTER = "Images (*.jpg *.jpeg *.png *.bmp *.tiff)"
EXPORT_FILTER = "SV Face ID employees (*.zip)"
SAMPLE_LIMIT = 8
MAX_PENDING_SAMPLES = MAX_ENROLLMENT_SAMPLES
AUTO_CAPTURE_COOLDOWN = 1.5
PREVIEW_INTERVAL_MS = 33
THUMB_SIZE = 56
PROFILE_PHOTO_SIZE = 130
GALLERY_THUMB_SIZE = 80
ENROLLMENT_PREVIEW_HEIGHT = 380
MAX_GALLERY_ITEMS = 12
ENROLLMENT_PHOTOS_DIR = "enrollment_photos"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".tiff")
DEFAULT_PAGE_SIZE = 25
HRM_AUTO_SYNC_SEC = 15 * 60     # Refresh the HRM list on open when the cache is older than this.
HRM_REFRESH_CHECK_MS = 5 * 60 * 1000
HRM_PHOTO_WORKERS = 4
# Enrollment filter entries, in status_filter order.
ENROLLMENT_FILTERS = (("All employees", hd.ANY), ("Ready for attendance", hd.READY),
                      ("Needs enrollment", hd.NEEDS), ("Possible duplicate faces", hd.DUPLICATE),
                      ("Active employees", hd.LOCAL_ACTIVE), ("Inactive employees", hd.LOCAL_INACTIVE),
                      ("Not in HRM", hd.NOT_IN_HRM))
NOT_IN_HRM_TIP = ("SV HRM doesn't list this employee as active, so their attendance isn't recorded. "
                  "They may have left, or their employee ID may not match.")


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

    def submit(self, images, name, employee_id, check_quality=True, first_name="", last_name=""):
        copies = [image.copy() for image in images if image is not None]
        thread = threading.Thread(target=self._work,
                                  args=(self.service, copies, name, employee_id, check_quality,
                                        first_name, last_name),
                                  name="enrollment", daemon=True)
        thread.start()

    def _work(self, service, images, name, employee_id, check_quality, first_name="", last_name=""):
        try:
            outcome = service.enroll_many(images, name, employee_id, check_quality,
                                          first_name=first_name, last_name=last_name)
        except Exception as exc:
            outcome = EnrollmentOutcome("error", str(exc), name=name, employee_id=employee_id or "")
        self.finished.emit(outcome)


class SampleRunner(QObject):
    """Validates one prospective sample off the Qt thread."""

    validated = pyqtSignal(object, object, object)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._busy = False

    @property
    def busy(self):
        return self._busy

    def submit(self, frame, check_quality, context=None):
        if self._busy or frame is None:
            return False
        self._busy = True
        thread = threading.Thread(target=self._work, args=(frame.copy(), check_quality, context, self.service),
                                  name="enroll-sample", daemon=True)
        thread.start()
        return True

    def _work(self, frame, check_quality, context, service):
        try:
            status, message, issues = service.inspect(frame, check_quality)
        except Exception as exc:
            status, message, issues = "error", str(exc), ()
        self._busy = False
        self.validated.emit((status, message, issues), frame, context)


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

def compose_full_name(first_name, last_name):
    return " ".join(part for part in (first_name.strip(), last_name.strip()) if part)


class NameFields(QObject):
    """First, last and full name inputs; the full name follows the parts until edited by hand."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.first = QLineEdit()
        self.last = QLineEdit()
        self.full = QLineEdit()
        for edit, hint in ((self.first, "e.g. Jane"), (self.last, "e.g. Smith"),
                           (self.full, "Required, e.g. Jane Smith")):
            edit.setMaxLength(100)
            edit.setPlaceholderText(hint)
        self._composed = ""
        self.first.textChanged.connect(self._compose)
        self.last.textChanged.connect(self._compose)

    def _compose(self):
        composed = compose_full_name(self.first.text(), self.last.text())
        if self.full.text().strip() == self._composed:
            self.full.setText(composed)
        self._composed = composed

    def add_to(self, grid, row=0):
        for offset, (label, edit) in enumerate((("First name", self.first), ("Last name", self.last),
                                                ("Full name", self.full))):
            grid.addWidget(QLabel(label), row + offset, 0)
            grid.addWidget(edit, row + offset, 1)
        return row + 3

    def set_values(self, first_name="", last_name="", full_name=""):
        self.first.setText(first_name)
        self.last.setText(last_name)
        self.full.setText(full_name)
        self._composed = compose_full_name(first_name, last_name)

    def values(self):
        return self.first.text().strip(), self.last.text().strip(), self.full.text().strip()

    def set_read_only(self, read_only):
        for edit in (self.first, self.last, self.full):
            edit.setReadOnly(read_only)


class EditNameDialog(QDialog):
    """Edit an employee's first, last and full name; the employee ID never changes."""

    def __init__(self, row, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit employee")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Employee ID: " + row["employee_id"]))
        form = QGridLayout()
        form.setSpacing(8)
        self.names = NameFields(self)
        self.names.add_to(form)
        self.names.set_values(row.get("first_name", ""), row.get("last_name", ""), row["employee_name"])
        layout.addLayout(form)
        hint = QLabel("Check-in speech says \"Thank you\" and the last name.")
        hint.setObjectName("screenSubtitle")
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setMinimumWidth(380)

    @classmethod
    def ask(cls, row, parent=None):
        """Return ``(first, last, full)`` or ``None`` when cancelled."""
        dialog = cls(row, parent)
        return dialog.names.values() if dialog.exec() == QDialog.DialogCode.Accepted else None


class EmployeeDialog(QDialog):
    def reject(self):
        owner = self.parent()
        if owner.saving:
            owner.editor_toast.show_message("Please wait for the current photo check to finish.", "info")
            return
        super().reject()


class EmployeesScreen(QWidget):
    probeReady = pyqtSignal(object)
    duplicatesReady = pyqtSignal(object)
    hrmSyncDone = pyqtSignal(object)
    anglesRequested = pyqtSignal(str)  # Employee code: enroll their face angles on AI Face Angles.
    hrmSyncProgress = pyqtSignal(int, int)
    hrmPhotoReady = pyqtSignal(str)
    hrmAnglesReady = pyqtSignal(str, object)  # employee code, (items, problems) or an exception

    def __init__(self, engine, settings, parent=None, preview_capture_factory=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._preview_factory = preview_capture_factory
        self.service = EnrollmentService(settings.encodings_path, settings.employees_path,
                                         duplicate_distance=settings.face_tolerance)
        self._duplicates_cached_key = None
        self._duplicates_cache = ()
        self._duplicates_by_label = {}
        self._duplicates_scan = None
        self._duplicates_closed = False
        self._duplicates_error = ""
        self.runner = EnrollmentRunner(self.service, self)
        self.pending = []
        self.saving = False
        self._editing_label = None
        self.upload = None
        self.rows = []                 # Local enrollments (one per label)
        self.directory_rows = []       # HRM employees merged with local enrollments
        self.filtered_rows = []
        self._page_rows = []           # The rows the table shows, by table row
        self.page = 0
        self.page_count = 1
        self._hrm_directory = hd.load(hd.cache_path(settings))
        self._hrm_syncing = False
        self._hrm_sync_error = ""
        self._hrm_photo_pending = set()
        self._hrm_photo_missing = set()
        self._hrm_photo_lock = threading.Lock()
        self.countdown = 0
        self.probing = False
        self.validating = False
        self._sample_context = None
        self._probe_context = None
        self._probe_busy = False
        self._preview_stale_sec = 2.0
        self._preview_sequence = None
        self._probe_sequence = None
        self._sample_digests = set()
        self.sample_runner = SampleRunner(self.service, self)
        self.last_auto_capture = 0.0
        self.preview_on = False
        self.preview_camera = None
        self._engine_was_running = False
        self._thumb_cache = {}
        self._captures_map = {}
        self._sample_photos_map = {}
        self._captures_dirty = True
        self._hrm_sync_again = False
        self._profile_angles_code = None      # Whose angles the profile shows.
        self._profile_angles_loading = None   # Whose angles are downloading now.
        self._profile_angles_again = False
        self._hrm_live = False
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self._build()
        self._connect()
        self.reload()
        self._auto_sync_hrm()
        # The Live Monitor reads this cache, so it stays current without this screen being open.
        self.hrm_refresh_timer = QTimer(self)
        self.hrm_refresh_timer.setInterval(HRM_REFRESH_CHECK_MS)
        self.hrm_refresh_timer.timeout.connect(self._auto_sync_hrm)
        self.hrm_refresh_timer.start()

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
        self.header = PageHeader("", "Manage people, enrollment photos and attendance access.")
        self.header.layout().setStretch(0, 1)
        self.enrolled_pill = StatusPill("0 enrolled", "idle", dot=True)
        self.reload_button = QPushButton("Refresh")
        self.hrm_sync_button = QPushButton("Sync from HRM")
        self.hrm_sync_button.setToolTip("Load every employee from SV HRM. Uses the HRM address and API key "
                                        "in Settings > AI Configuration.")
        self.folder_button = QPushButton("Open data folder")
        self.folder_button.hide()
        self.enroll_cta = QPushButton("Add employee")
        self.enroll_cta.setObjectName("primary")
        self.more_button = QPushButton("More")
        self.more_button.setProperty("overflowMenu", True)
        self.more_button.setToolTip("Import or export enrolled employees")
        more = QMenu(self)
        self.import_action = more.addAction("Import employees…", self._import_employees)
        self.export_action = more.addAction("Export employees…", self._export_employees)
        self.more_button.setMenu(more)
        self.header.add_action(self.hrm_sync_button)
        self.header.add_action(self.reload_button)
        self.header.add_action(self.more_button)
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
        tools.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search name, employee ID, department or position")
        self.search_edit.setClearButtonEnabled(True)
        self.status_filter = QComboBox()
        for label, _ in ENROLLMENT_FILTERS:
            self.status_filter.addItem(label)
        self.status_filter.setMinimumWidth(180)
        self.status_filter.setToolTip("Face enrollment status")
        tools.addWidget(self.search_edit, 1)
        tools.addWidget(self.status_filter)
        card.add_layout(tools)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.department_filter = self._filter_combo("All departments", "Department in HRM")
        self.position_filter = self._filter_combo("All positions", "Position in HRM")
        self.location_filter = self._filter_combo("All locations", "Work location in HRM")
        self.employee_type_filter = self._filter_combo("All employee types", "Employee type in HRM")
        self.clear_filters_button = QPushButton("Clear filters")
        for widget in (self.department_filter, self.position_filter, self.location_filter, self.employee_type_filter):
            widget.setMinimumWidth(150)
            filters.addWidget(widget, 1)
        filters.addWidget(self.clear_filters_button)
        card.add_layout(filters)

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
        for col, width in ((0, 80), (2, 170), (3, 160), (4, 85), (5, 90)):
            self.employee_table.setColumnWidth(col, width)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(80)
        card.add(self.employee_table, 1)
        self.empty_label = QLabel("No employees yet. Click Add employee to enroll your first person.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("screenSubtitle")
        card.add(self.empty_label)

        pager = QHBoxLayout()
        pager.setSpacing(6)
        self.result_count = QLabel()
        self.result_count.setObjectName("screenSubtitle")
        pager.addWidget(self.result_count, 1)
        pager.addWidget(QLabel("Rows per page"))
        self.page_size_combo = QComboBox()
        for size in hd.PAGE_SIZES:
            self.page_size_combo.addItem(str(size), size)
        self.page_size_combo.setCurrentIndex(hd.PAGE_SIZES.index(DEFAULT_PAGE_SIZE))
        pager.addWidget(self.page_size_combo)
        self.first_page_button = QPushButton("«")
        self.prev_page_button = QPushButton("‹ Prev")
        self.page_label = QLabel("Page 1 of 1")
        self.page_label.setMinimumWidth(90)
        self.page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.next_page_button = QPushButton("Next ›")
        self.last_page_button = QPushButton("»")
        self.first_page_button.setToolTip("First page")
        self.last_page_button.setToolTip("Last page")
        for widget in (self.first_page_button, self.prev_page_button, self.page_label,
                       self.next_page_button, self.last_page_button):
            pager.addWidget(widget)
        card.add_layout(pager)

        self.hrm_status_label = QLabel()
        self.hrm_status_label.setObjectName("screenSubtitle")
        self.hrm_status_label.setWordWrap(True)
        card.add(self.hrm_status_label)
        self.duplicate_review = QLabel()
        self.duplicate_review.setObjectName("screenSubtitle")
        self.duplicate_review.setWordWrap(True)
        card.add(self.duplicate_review)
        return card

    @staticmethod
    def _filter_combo(any_label, tooltip):
        combo = QComboBox()
        combo.setToolTip(tooltip)
        combo.addItem(any_label, hd.ANY)
        return combo

    def _build_wizard(self):
        card = Card("Add employee", "Enter their details, then capture or upload a clear face photo.", icon="user-plus", theme=self._theme)
        self.wizard_card = card

        form = QGridLayout()
        form.setSpacing(8)
        self.names = NameFields(self)
        self.first_name_edit, self.last_name_edit, self.name_edit = \
            self.names.first, self.names.last, self.names.full
        row = self.names.add_to(form)
        form.addWidget(QLabel("Employee ID"), row, 0)
        self.id_edit = QLineEdit()
        self.id_edit.setMaxLength(100)
        self.id_edit.setPlaceholderText("Optional, e.g. EMP-125")
        form.addWidget(self.id_edit, row, 1)
        self.quality_check = QCheckBox("Reject low-quality samples")
        self.quality_check.setChecked(True)
        form.addWidget(self.quality_check, row + 1, 0, 1, 2)
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

        self.hint = QLabel("Add front-facing, slight left/right, and slight up/down views of the same person. "
                           "Keep both eyes visible. Auto stops at 8 samples; Add samples appends more. "
                           "Identity consistency is always checked when saving, even with quality checks off. "
                           "Samples from another person are rejected. Every accepted photo is saved; "
                           "the first available sample is the directory image.")
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
        self.capture_video.setMinimumHeight(ENROLLMENT_PREVIEW_HEIGHT)
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
        self.drop.setMinimumHeight(ENROLLMENT_PREVIEW_HEIGHT)
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
        card.body.setAlignment(Qt.AlignmentFlag.AlignTop)

        header = QHBoxLayout()
        header.setSpacing(16)
        self.profile_photo = QLabel()
        self.profile_photo.setFixedSize(PROFILE_PHOTO_SIZE, PROFILE_PHOTO_SIZE)
        self.profile_photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.profile_photo.setCursor(Qt.CursorShape.PointingHandCursor)
        self.profile_photo.mousePressEvent = self._profile_photo_clicked
        header.addWidget(self.profile_photo, 0, Qt.AlignmentFlag.AlignTop)

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

        angles_header = QHBoxLayout()
        angles_title = QLabel("Enrolled angles")
        angles_title.setObjectName("sectionTitle")
        self.angles_count_label = QLabel()
        self.angles_count_label.setObjectName("screenSubtitle")
        angles_header.addWidget(angles_title)
        angles_header.addStretch()
        angles_header.addWidget(self.angles_count_label)
        card.add_layout(angles_header)
        self.angles_scroll = QScrollArea()
        self.angles_scroll.setWidgetResizable(True)
        self.angles_scroll.setObjectName("panel")
        self.angles_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.angles_scroll.setFixedHeight(GALLERY_THUMB_SIZE + 84)
        self.angles_container = QWidget()
        self.angles_layout = QHBoxLayout(self.angles_container)
        self.angles_layout.setContentsMargins(8, 8, 8, 8)
        self.angles_layout.setSpacing(8)
        self.angles_scroll.setWidget(self.angles_container)
        card.add(self.angles_scroll)
        self.angles_hint = QLabel()
        self.angles_hint.setObjectName("screenSubtitle")
        self.angles_hint.setWordWrap(True)
        card.add(self.angles_hint)

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
        self.gallery_scroll.setFixedHeight(GALLERY_THUMB_SIZE + 68)
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
        # Faces are enrolled as angles; these two only ever changed local samples.
        self.p_add_btn.hide()
        self.p_del_btn.hide()
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
        card.body.addStretch(1)

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
        self.duplicatesReady.connect(self._on_duplicates_ready)

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
        self.reload_button.clicked.connect(self._refresh_directory)
        self.hrm_sync_button.clicked.connect(lambda: self.sync_hrm(quiet=False))
        self.hrmSyncDone.connect(self._on_hrm_synced)
        self.hrmSyncProgress.connect(self._on_hrm_progress)
        self.hrmPhotoReady.connect(self._on_hrm_photo)
        self.hrmAnglesReady.connect(self._on_profile_angles)
        self.folder_button.clicked.connect(self._open_folder)
        self.enroll_cta.clicked.connect(self._switch_to_wizard)

        # Segment control
        self.seg_capture.clicked.connect(lambda: self._switch_segment(0))
        self.seg_upload.clicked.connect(lambda: self._switch_segment(1))

        # Search, filters and pages. Typing waits briefly so each keystroke doesn't rebuild the table.
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(lambda: self._filter_employees(self.search_edit.text()))
        self.search_edit.textChanged.connect(lambda _: self.search_timer.start())
        for combo in (self.status_filter, self.department_filter, self.position_filter,
                      self.location_filter, self.employee_type_filter):
            combo.currentIndexChanged.connect(lambda _: self._filter_employees(self.search_edit.text()))
        self.clear_filters_button.clicked.connect(self._clear_filters)
        self.page_size_combo.currentIndexChanged.connect(lambda _: self._go_to_page(0))
        self.first_page_button.clicked.connect(lambda: self._go_to_page(0))
        self.prev_page_button.clicked.connect(lambda: self._go_to_page(self.page - 1))
        self.next_page_button.clicked.connect(lambda: self._go_to_page(self.page + 1))
        self.last_page_button.clicked.connect(lambda: self._go_to_page(self.page_count - 1))

        # Employee list
        self.employee_table.cellDoubleClicked.connect(lambda *_: self._activate_selected())

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
        if self.capture_stack.currentIndex() != index:
            self._invalidate_checks()
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

    def _filters(self, text=None):
        return hd.Filters(search=self.search_edit.text() if text is None else text,
                          department=self.department_filter.currentData() or hd.ANY,
                          position=self.position_filter.currentData() or hd.ANY,
                          location=self.location_filter.currentData() or hd.ANY,
                          employee_type=self.employee_type_filter.currentData() or hd.ANY,
                          enrollment=ENROLLMENT_FILTERS[max(0, self.status_filter.currentIndex())][1])

    def _filter_employees(self, text):
        """Filters changed: show the first page of the new result."""
        self.search_timer.stop()
        self._apply_filters(text, page=0)

    def _apply_filters(self, text=None, page=None):
        self.filtered_rows = self._filters(text).apply(self.directory_rows, self._duplicates_by_label)
        self._go_to_page(self.page if page is None else page)

    def _clear_filters(self):
        widgets = (self.search_edit, self.status_filter, self.department_filter, self.position_filter,
                   self.location_filter, self.employee_type_filter)
        for widget in widgets:
            widget.blockSignals(True)
        self.search_edit.clear()
        for combo in widgets[1:]:
            combo.setCurrentIndex(0)
        for widget in widgets:
            widget.blockSignals(False)
        self._filter_employees("")

    def _page_size(self):
        return int(self.page_size_combo.currentData() or DEFAULT_PAGE_SIZE)

    def _go_to_page(self, page):
        rows, self.page, self.page_count = hd.paginate(self.filtered_rows, page, self._page_size())
        self._render_page(rows)
        total, size = len(self.filtered_rows), self._page_size()
        first = self.page * size + 1 if total else 0
        last = min(total, (self.page + 1) * size)
        enrolled = sum(1 for row in self.directory_rows if hd.face_count(row))
        self.result_count.setText(
            f"Showing {first}–{last} of {total} employees"
            + (f" (filtered from {len(self.directory_rows)})" if total != len(self.directory_rows) else "")
            + f" • {enrolled} enrolled • Double-click a row to open it")
        self.page_label.setText(f"Page {self.page + 1} of {self.page_count}")
        self.first_page_button.setEnabled(self.page > 0)
        self.prev_page_button.setEnabled(self.page > 0)
        self.next_page_button.setEnabled(self.page < self.page_count - 1)
        self.last_page_button.setEnabled(self.page < self.page_count - 1)
        self.clear_filters_button.setEnabled(self._filters().active)
        self.empty_label.setText("No employees match your filters." if self.directory_rows else
                                 "No employees yet. Click Sync from HRM, or Add employee to enroll your first person.")
        self.empty_label.setVisible(not rows)

    def _set_filter_options(self, combo, values):
        current = combo.currentData() or hd.ANY
        combo.blockSignals(True)
        while combo.count() > 1:
            combo.removeItem(1)
        for value in values:
            combo.addItem(value, value)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)
        combo.setEnabled(bool(values))

    def _apply_icons(self):
        colors = palette(self._theme)
        apply_button_icon(self.reload_button, "refresh", colors["muted"])
        apply_button_icon(self.more_button, "more", colors["muted"])
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
        return self._enrollment_photo_dir() / portrait_name(label)

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
        for photo in self._sample_photos_map.get(label, []):
            if photo is not None and photo.is_file():
                return photo
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
        if self.saving:
            return
        if self.preview_on:
            self._stop_preview()
        self._clear_samples()
        self.editor_toast.hide_message()
        self.editor_toast.hide_message()
        self._editing_label = row["name"] if row else None
        self.names.set_values(row.get("first_name", "") if row else "", row.get("last_name", "") if row else "",
                              row["employee_name"] if row else "")
        self.id_edit.setText(row["employee_id"] if row else "")
        self.names.set_read_only(row is not None)
        self.id_edit.setReadOnly(row is not None)
        title = "Update employee photo" if row else "Add employee"
        self.wizard_card.title_label.setText(title)
        self.wizard_card.header.layout().setStretch(1, 1)
        self.editor.setWindowTitle(title)
        self.wizard_card.set_subtitle("Add more views of this employee. Every accepted sample photo is saved."
                                     if row else "Enter their details, then capture or upload a clear face photo.")
        self.save_button.setText("Save photos" if row else "Add employee")
        self.right_stack.setCurrentIndex(0)
        self.editor.show()
        self.coach.setText("Start the preview to capture a face, or choose Upload Photo.")
        self.first_name_edit.setFocus()

    def _editor_closed(self, _result):
        self.countdown_timer.stop()
        self.countdown = 0
        self.capture_button.setText("Capture sample")
        if self.preview_on:
            self._stop_preview()
        self._clear_samples()
        self._editing_label = None

    def _switch_to_profile(self):
        self._invalidate_checks()
        if self.preview_on:
            self._stop_preview()
        self.right_stack.setCurrentIndex(1)

    def _show_profile(self):
        row = self.selected_row()
        if row:
            self._populate_profile(row)
            self._switch_to_profile()
            self.editor.setWindowTitle("Employee details")
            self.editor.show()

    def _populate_profile(self, row, refresh_photos=True):
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
        self._profile_row = dict(row)
        self._profile_captures = tuple(captures)
        self.profile_name.setText(row["employee_name"])
        self.profile_id_label.setText(
            f"ID: {row['employee_id']}" if row["employee_id"] else "No employee ID")
        duplicates = self._duplicates_by_label.get(row["name"], [])
        self.profile_pill.set_status(
            "Inactive" if not row.get("active", True) else "Not in HRM" if row.get("hrm_blocked") else "Review face match" if duplicates else ("Active" if hd.face_count(row) else "Needs enrollment"),
            "bad" if not row.get("active", True) or row.get("hrm_blocked") else "warn" if duplicates or not hd.face_count(row) else "ok")
        lines = [f"First name: {row.get('first_name') or '—'}  •  Last name: {row.get('last_name') or '—'}"]
        if duplicates:
            lines.append("Possible duplicate face with " + "; ".join(owner for _, owner in duplicates))
            lines.append("Compare both profiles. Delete only a confirmed mistaken enrollment using its directory action.")
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

        if refresh_photos:
            self._load_profile_angles(row)
            # -- gallery shows evidence captures --
            self._populate_gallery(captures)

    # --- enrolled angles, from SV HRM ---------------------------------------

    def _load_profile_angles(self, row):
        code = str(row.get("employee_id") or "").strip()
        if code != self._profile_angles_code:
            self._profile_angles_code = code
            self._render_profile_angles({})
            self.angles_count_label.setText("")
        config = self._hrm_config()
        if not row.get("in_hrm"):
            self.angles_hint.setText("This employee isn't in SV HRM, so there are no saved angles to show.")
            return
        if hd.hrm_sync.configuration_problem(config):
            self.angles_hint.setText("Add the HRM address and API key in Settings > AI Configuration to see saved angles.")
            return
        if self._profile_angles_loading == code:
            return
        self._profile_angles_loading = code
        self.angles_hint.setText("Loading angles from SV HRM…")
        settings = self.settings

        def work():
            try:
                result = hd.fetch_saved_angles(config, settings, code)
            except (HRMError, OSError) as exc:
                result = exc
            try:
                self.hrmAnglesReady.emit(code, result)
            except RuntimeError:
                pass  # The screen was closed meanwhile.

        threading.Thread(target=work, name="hrm-profile-angles", daemon=True).start()

    def _on_profile_angles(self, code, result):
        if self._profile_angles_loading == code:
            self._profile_angles_loading = None
        again, self._profile_angles_again = self._profile_angles_again, False
        if code != self._profile_angles_code:
            # Another employee is on screen now; start the download skipped while this one ran.
            row = getattr(self, "_profile_row", None)
            if row and self._profile_angles_loading is None and str(row.get("employee_id") or "").strip() == self._profile_angles_code:
                self._load_profile_angles(row)
            return
        if isinstance(result, Exception):
            self.angles_hint.setText(f"Couldn't load angles from SV HRM. {result}")
        else:
            items, problems = result
            self._render_profile_angles({item["key"]: item for item in items})
            result = angle_checks.score(item.get("check") for item in items)
            self.angles_count_label.setText(f"{len(items)} of {len(ANGLES)} in SV HRM · "
                                            f"{result.verified_percent}% verified")
            if problems:
                self.angles_hint.setText(f"{len(problems)} angle(s) couldn't be loaded from SV HRM.")
            elif items:
                unusable = angle_templates.problems_for(angle_templates.load(
                    angle_templates.store_path(self.settings)), code)
                self.angles_hint.setText(
                    "Can't be matched on the Live Monitor: " + "; ".join(
                        f"{angle.capitalize()} ({why})" for angle, why in unusable.items()) if unusable else
                    "The Live Monitor recognizes this employee from these angles. Click one to view it full size.")
            else:
                self.angles_hint.setText("No angles saved in SV HRM yet. Generate them in AI Face Angles.")
        if again:
            row = getattr(self, "_profile_row", None)
            if row:
                self._load_profile_angles(row)

    def _render_profile_angles(self, items):
        while self.angles_layout.count():
            item = self.angles_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for angle in ANGLES:
            saved = items.get(angle.key)
            tile = QWidget()
            tile.setFixedWidth(96)
            column = QVBoxLayout(tile)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(6)
            photo = QLabel()
            photo.setFixedSize(GALLERY_THUMB_SIZE, GALLERY_THUMB_SIZE)
            photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            photo.setObjectName("panel")
            photo.setAccessibleName(f"Enrolled angle: {angle.label}")
            # Not from the thumbnail cache: a replaced angle keeps the same file name.
            pixmap = QPixmap(str(saved["path"])) if saved else None
            if pixmap is not None and not pixmap.isNull():
                photo.setPixmap(_rounded_pixmap(pixmap, GALLERY_THUMB_SIZE))
                photo.setCursor(Qt.CursorShape.PointingHandCursor)
                photo.setToolTip(f"{angle.label} · Click to view full size")
                photo.mousePressEvent = lambda _event, p=saved["path"]: self._open_image(p)
            else:
                photo.setText("Not\nenrolled")
                photo.setToolTip(f"{angle.label} isn't saved in SV HRM yet.")
            column.addWidget(photo, 0, Qt.AlignmentFlag.AlignHCenter)
            caption = QLabel(f"{angle.key.capitalize()}\n{angle.label_km}" if angle.label_km else angle.key.capitalize())
            caption.setObjectName("screenSubtitle")
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption.setWordWrap(True)
            column.addWidget(caption)
            check = saved.get("check") if saved else None
            if angle_checks.is_warning(check):
                warning = QLabel(f"⚠ {angle_checks.SHORT_WARNINGS[check]}")
                warning.setStyleSheet(f"color: {angle_checks.WARNING_COLOR}; font-weight: 600;")
                warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
                warning.setWordWrap(True)
                warning.setToolTip(angle_checks.WARNINGS[check])
                column.addWidget(warning)
            self.angles_layout.addWidget(tile)
        self.angles_layout.addStretch()

    def _angle_counts(self):
        """{code: usable angles} once connected to HRM; None means matching still uses local samples."""
        directory = self._hrm_directory
        if not directory.synced or not directory.employees:
            return None
        return angle_templates.counts(angle_templates.load(angle_templates.store_path(self.settings)))

    def on_angle_templates_updated(self, result):
        """The Live Monitor's templates were rebuilt from HRM's angles."""
        if result.changed:
            self.reload(refresh_profile_photos=False)

    def on_hrm_angles_changed(self, code):
        """HRM says this employee's angles changed; refresh the profile if it's open."""
        row = getattr(self, "_profile_row", None)
        if (not row or not self.editor.isVisible() or self.right_stack.currentIndex() != 1
                or str(row.get("employee_id") or "").casefold() != str(code).casefold()):
            return
        if self._profile_angles_loading == row["employee_id"]:
            self._profile_angles_again = True
        else:
            self._load_profile_angles(row)

    def on_hrm_photo_changed(self, code):
        """HRM has a new photo for this employee; replace the saved copy behind the table's thumbnail."""
        config = self._hrm_config()
        if not config.configured:
            return
        settings = self.settings

        def work():
            try:
                hd.fetch_photo(config, settings, code)
            except (HRMError, OSError) as exc:
                logging.info("Could not refresh the HRM photo for %s: %s", code, exc)
                return
            try:
                self.hrmPhotoReady.emit(code)
            except RuntimeError:
                pass  # The screen was closed meanwhile.

        threading.Thread(target=work, name="hrm-photo-changed", daemon=True).start()

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
            tile = QWidget()
            tile.setFixedWidth(104)
            column = QVBoxLayout(tile)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(6)
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
            column.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)
            delete = QPushButton("Delete")
            delete.setObjectName("danger")
            delete.setProperty("evidenceDelete", True)
            delete.setAccessibleName(f"Delete evidence capture {path.name}")
            delete.setToolTip("Delete this evidence image and its full-frame companion")
            delete.setFixedHeight(32)
            apply_button_icon(delete, "trash", palette(self._theme)["danger"], size=14)
            delete.clicked.connect(lambda _checked=False, p=path: self._delete_evidence(p))
            column.addWidget(delete)
            self.gallery_layout.addWidget(tile)

        self.gallery_layout.addStretch()

        remaining = len(captures) - len(shown)
        if remaining > 0:
            self.gallery_hint.setText(
                f"Showing {len(shown)} of {len(captures)} captures. "
                "Click any photo to view full size.")
        else:
            self.gallery_hint.setText("Click any photo to view full size")

    def _delete_evidence(self, path):
        row = getattr(self, "_profile_row", None)
        if row is None or path not in getattr(self, "_profile_captures", ()):
            return
        answer = QMessageBox.question(
            self.editor, "Delete evidence capture",
            f"Permanently delete this capture for {row['employee_name']}?\n\n{path.name}\n\n"
            "The face crop, full-frame image and capture metadata will be removed. "
            "The attendance record and enrolled face samples will be kept.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            removed = delete_capture(path, self.settings.capture_dir)
        except (OSError, ValueError) as exc:
            self._notify(f"Could not finish deleting the capture: {exc}", "bad")
        else:
            self._notify("Evidence capture deleted." if removed else "This capture was already removed.", "ok")
        self._invalidate_captures()
        self.reload()
        self._populate_profile(row)

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

    def _latest_preview_packet(self):
        packet = self.preview_camera.frames.get() if self.preview_camera is not None else None
        if packet is None or time.monotonic() - packet.captured_at > self._preview_stale_sec:
            return None
        return packet

    def _latest_preview_frame(self):
        packet = self._latest_preview_packet()
        return None if packet is None else packet.frame

    def _pull_preview(self):
        if not self.preview_on:
            return
        packet = self._latest_preview_packet()
        if packet is None:
            self.capture_video.show_message("Waiting for camera", "No fresh camera frame available")
            return
        sequence = (packet.generation, packet.sequence)
        if sequence != self._preview_sequence:
            self._preview_sequence = sequence
            self.capture_video.set_frame(packet.frame)

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
        self._preview_stale_sec = config.camera_stale_sec
        self._preview_sequence = self._probe_sequence = None
        self.preview_camera.start()
        self.preview_on = True
        self.preview_button.setText("Stop preview")
        self.capture_button.setEnabled(True)
        self.preview_timer.start()
        self.probe_timer.start()
        self.coach.setText("Preview running on the enrollment camera. "
                           "Face the camera, then capture samples.")

    def _stop_preview(self, resume=True):
        self._invalidate_checks()
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
        if (self.saving or self._probe_busy or self.probing or self.validating or not self.preview_on
                or self.preview_camera is None):
            return
        packet = self._latest_preview_packet()
        if packet is None or (packet.generation, packet.sequence) == self._probe_sequence:
            return
        self._probe_sequence = (packet.generation, packet.sequence)
        context = (object(), packet.captured_at, packet.generation)
        self._probe_context = context
        self.probing = True
        self._probe_busy = True
        # Snapshot every UI/config value here; workers never access Qt widgets.
        threading.Thread(target=self._probe, args=(packet.frame.copy(), context,
                         max(0.1, float(self.settings.detection_scale)),
                         self.quality_check.isChecked(), self.service), name="enroll-probe",
                         daemon=True).start()

    def _probe(self, frame, context, scale, check_quality, service):
        def emit(payload):
            self._probe_busy = False
            self.probeReady.emit((context, payload))
        try:
            small = cv2.resize(frame, (max(1, round(frame.shape[1] * scale)),
                                       max(1, round(frame.shape[0] * scale))),
                               interpolation=cv2.INTER_AREA)
            with FACE_BACKEND_LOCK:
                boxes = service._detector().face_locations(
                    cv2.cvtColor(small, cv2.COLOR_BGR2RGB), model="yunet")
            if not boxes:
                emit(("no_face", "No face detected. Step into the frame.", None, (), None))
                return
            if len(boxes) > 1:
                emit(("multiple_faces",
                                      f"{len(boxes)} faces in view. Enroll one person per sample.",
                                      None, (), None))
                return
            top, right, bottom, left = boxes[0]
            box = (top / scale, right / scale, bottom / scale, left / scale)
            issues = ()
            if check_quality:
                from ...enrollment import frame_quality
                issues = frame_quality(frame, box)
            emit(("ok", "", box, issues, frame))

        except Exception as exc:
            emit(("error", f"Face detection failed: {exc}", None, (), None))

    def on_probe(self, payload):
        context, payload = payload
        if context is not self._probe_context:
            return
        self._probe_context = None
        self.probing = False
        if self.saving or not self.preview_on:
            return
        latest = self._latest_preview_packet()
        if (latest is None or latest.generation != context[2]
                or time.monotonic() - context[1] > self._preview_stale_sec):
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
        if not self.preview_on or self.saving or not self.auto_check.isChecked() or self.validating or self.countdown:
            return
        if len(self.pending) >= SAMPLE_LIMIT:
            return
        if time.monotonic() - self.last_auto_capture < AUTO_CAPTURE_COOLDOWN:
            return
        self.last_auto_capture = time.monotonic()
        self._submit_sample(frame)

    def _start_countdown(self):
        if self.countdown or self.saving or self.validating or not self.preview_on:
            return
        seconds = self.countdown_box.value()
        if seconds <= 0:
            self._grab_sample()
            return
        self.countdown = seconds
        self.capture_button.setEnabled(False)
        self.capture_button.setText(f"Capturing in {self.countdown}...")
        self.countdown_timer.start()

    def _tick_countdown(self):
        if not self.preview_on or self.countdown <= 0:
            self._cancel_countdown()
            return
        self.countdown -= 1
        if self.countdown <= 0:
            self.countdown_timer.stop()
            self.capture_button.setEnabled(self.preview_on)
            self.capture_button.setText("Capture sample")
            self._grab_sample()
            return
        self.capture_button.setText(f"Capturing in {self.countdown}...")

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
        return self._validate_sample(frame, "live")

    def _validate_sample(self, frame, source):
        if len(self.pending) >= MAX_PENDING_SAMPLES:
            self._notify(f"Save these samples before collecting more ({MAX_PENDING_SAMPLES} per save).", "warn")
            self.capture_button.setText("Capture sample")
            self.capture_button.setEnabled(self.preview_on and not self.countdown)
            return False
        digest = hashlib.sha256(frame.tobytes()).digest()
        if digest in self._sample_digests:
            self.coach.setText("This frame is already collected. Change pose and wait for a new frame.")
            self.capture_button.setText("Capture sample")
            self.capture_button.setEnabled(self.preview_on and not self.countdown)
            return False
        context = (object(), source, digest)
        if not self.sample_runner.submit(frame, self.quality_check.isChecked(), context):
            self.capture_button.setText("Capture sample")
            self.capture_button.setEnabled(self.preview_on and not self.countdown)
            return False
        self._sample_context = context
        self.validating = True
        return True

    def on_sample_validated(self, result, frame, context):
        if context is not self._sample_context:
            return
        self._sample_context = None
        self.validating = False
        status, message, issues = result
        upload_mode = context[1] == "upload"
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
        self._sample_digests.add(context[2])
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
        image = read_image(path)
        if image is None:
            self._notify(f"Could not read {path}", "bad")
            return
        self._invalidate_checks()
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
        if self._validate_sample(self.upload, "upload"):
            self.add_upload_button.setEnabled(False)
            self.add_upload_button.setText("Checking photo...")

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
        if self.saving:
            return
        self._invalidate_checks()
        self.pending.clear()
        self._sample_digests.clear()
        self.last_auto_capture = 0.0
        self.upload = None
        self.drop.clear()
        self.drop.setText("Drop a photo here\nor choose one below")
        self.add_upload_button.setEnabled(False)
        self._refresh_samples()
        self.coach.setText("Samples cleared.")

    def _cancel_countdown(self):
        self.countdown_timer.stop()
        self.countdown = 0
        self.capture_button.setText("Capture sample")
        self.capture_button.setEnabled(self.preview_on and not self.validating)

    def _invalidate_checks(self):
        """Ignore queued work belonging to a previous editor/camera state."""
        self._sample_context = self._probe_context = None
        self.validating = self.probing = False
        self._cancel_countdown()
        self.add_upload_button.setText("Add photo as sample")
        self.add_upload_button.setEnabled(self.upload is not None)

    def _save(self):
        if self.saving or self.validating:
            return
        first_name, last_name, full_name = self.names.values()
        name = self._editing_label or full_name
        if not name:
            self._notify("Enter the employee full name.", "warn")
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
        self.clear_button.setEnabled(False)
        self.probe_timer.stop()
        self.countdown_timer.stop()
        self.countdown = 0
        self.right_stack.setEnabled(False)
        # Keep a reference so we can save an enrollment photo after success.
        self._enrollment_save_samples = [s.copy() for s in self.pending]
        self.save_button.setEnabled(False)
        self.save_button.setText("Saving...")
        self._notify("Encoding samples...", "info")
        # Updating photos keeps the stored name parts; the fields are read-only then.
        self.runner.submit(self.pending, name, self.id_edit.text().strip(),
                           self.quality_check.isChecked(),
                           *(("", "") if self._editing_label else (first_name, last_name)))

    def on_enrollment_finished(self, outcome):
        self.saving = False
        self.clear_button.setEnabled(True)
        self.right_stack.setEnabled(True)
        self.save_button.setText("Save photos" if self._editing_label else "Add employee")
        self.save_button.setEnabled(bool(self.pending))
        if not outcome.ok:
            self._enrollment_save_samples = []
            message = outcome.message
            if outcome.status == "duplicate_face" and getattr(outcome, "conflicts", ()):
                owners = sorted({self._duplicate_owner(match, matched=True) for match in outcome.conflicts})
                message = ("This face is already enrolled as " + "; ".join(owners) + ". "
                           "Use Update photo on the existing employee. No enrollment was saved; "
                           "your collected samples are still available.")
            self.editor_toast.show_message(message, "bad")
            if self.preview_on:
                self.probe_timer.start()
            return
        # New enrollment persists each accepted photo in the worker alongside
        # its encoding. Keep compatibility with older/custom enrollment runners.
        photo_saved = bool(outcome.photo_paths) or self._save_enrollment_photo(
            outcome.name, getattr(self, "_enrollment_save_samples", []))
        self._enrollment_save_samples = []
        self.pending.clear()
        self._refresh_samples()
        self._notify(f"{outcome.message}. Total samples: {outcome.total_samples}", "ok")
        self.names.set_values()
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

    def _duplicate_catalog_key(self):
        """Cheap identity of the files used by an asynchronous duplicate scan."""
        signatures = []
        for value in (self.settings.encodings_path, self.settings.employees_path):
            path = Path(value).resolve()
            try:
                stat = path.stat()
                signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except OSError:
                signature = None
            signatures.append((str(path), signature))
        return tuple(signatures), self.settings.face_tolerance

    def _refresh_directory(self):
        self._duplicates_cached_key = None
        self.reload()

    @staticmethod
    def _duplicate_owner(match, matched=False):
        prefix = "matched_" if matched else ""
        name = getattr(match, prefix + "employee_name") or getattr(match, prefix + "label")
        employee_id = getattr(match, prefix + "employee_id")
        return f"{name} ({employee_id})" if employee_id else name

    def _request_duplicate_scan(self, key):
        if self._duplicates_closed or self._duplicates_scan is not None or key == self._duplicates_cached_key:
            return
        token = object()
        self._duplicates_scan = token
        service = self.service

        def work():
            try:
                matches, error = tuple(service.duplicate_faces()), ""
            except Exception as exc:
                matches, error = (), str(exc)
            self.duplicatesReady.emit((token, key, matches, error))

        threading.Thread(target=work, name="enrollment-duplicates", daemon=True).start()

    def _on_duplicates_ready(self, payload):
        token, key, matches, error = payload
        if token is not self._duplicates_scan or self._duplicates_closed:
            return
        self._duplicates_scan = None
        current = self._duplicate_catalog_key()
        if key != current:
            self._request_duplicate_scan(current)
            return
        self._duplicates_cached_key = key
        self._duplicates_cache = matches
        self._duplicates_error = error
        self.reload(refresh_profile_photos=False)

    def _show_duplicate_profile(self, label):
        for row in self.directory_rows:
            if row.get("name") == label:
                if not self.select_row(row):
                    self._clear_filters()
                    self.select_row(row)
                self._populate_profile(row)
                self._switch_to_profile()
                self.editor.setWindowTitle("Employee details — duplicate-face review")
                self.editor.show()
                return
        self._notify("This enrollment changed. Refresh the directory and review again.", "warn")

    def reload(self, refresh_profile_photos=True):
        key = self._duplicate_catalog_key()
        self._duplicates_by_label = {}
        if key == self._duplicates_cached_key:
            for match in self._duplicates_cache:
                self._duplicates_by_label.setdefault(match.label, []).append((match.matched_label, self._duplicate_owner(match, True)))
                self._duplicates_by_label.setdefault(match.matched_label, []).append((match.label, self._duplicate_owner(match)))
        self._sample_photos_map = sample_photo_map(self.settings.encodings_path)
        if self._captures_dirty:
            self._captures_map = self._scan_captures()
            self._captures_dirty = False
        self._thumb_cache.clear()
        self.rows = self.service.employees()
        angle_counts = self._angle_counts()
        self.directory_rows = hd.merge(self.rows, self._hrm_directory, angle_counts)
        matching_on_angles = angle_counts is not None
        self.employee_table.horizontalHeaderItem(4).setText("ANGLES" if matching_on_angles else "SAMPLES")
        self.stat_samples.title_label.setText("Face angles" if matching_on_angles else "Face samples")
        self.stat_samples.set_hint("Enrolled angles available for matching" if matching_on_angles
                                   else "Face samples available for matching")
        self._set_filter_options(self.department_filter, hd.options(self.directory_rows, "department"))
        self._set_filter_options(self.position_filter, hd.options(self.directory_rows, "position"))
        self._set_filter_options(self.location_filter, hd.options(self.directory_rows, "location"))
        self._set_filter_options(self.employee_type_filter, hd.options(self.directory_rows, "employee_type"))
        enrolled = {row["employee_id"] for row in self.directory_rows if hd.face_count(row)}
        review = {row["employee_id"] for row in self.rows if row["name"] in self._duplicates_by_label}
        inactive = {row["employee_id"] for row in self.rows if not row.get("active", True)}
        ready = len(enrolled-review-inactive)
        pending = len({row["employee_id"] for row in self.directory_rows
                       if not hd.face_count(row) and row.get("active", True)} - enrolled)
        self.stat_enrolled.set_value(ready, "warn" if review else "ok" if ready else "idle")
        self.stat_enrolled.set_hint(f"{len(review)} employees need face-match review" if review
                                    else f"{len(inactive)} inactive · active employees ready for attendance")
        self.stat_latest.set_value(pending, "warn" if pending else "idle")
        self.stat_samples.set_value(sum(hd.face_count(row) for row in self.directory_rows))
        self.enrolled_pill.set_status(f"{len(enrolled)} enrolled", "warn" if review else "ok" if ready else "idle")
        self._apply_filters()
        self._update_hrm_status()
        if key != self._duplicates_cached_key:
            self.duplicate_review.setText("Checking enrolled faces for possible duplicate employees…")
        elif self._duplicates_error:
            self.duplicate_review.setText("Duplicate-face review unavailable: " + self._duplicates_error
                                          + ". Refresh to retry.")
        elif self._duplicates_by_label:
            self.duplicate_review.setText(
                f"{len(self._duplicates_by_label)} enrollment profiles need face-match review. "
                "Use Edit → Review match to inspect the other employee. "
                "No profiles are merged or deleted automatically.")
        else:
            self.duplicate_review.setText("No cross-employee face matches found.")
        self._request_duplicate_scan(key)
        if self.editor.isVisible() and self.right_stack.currentIndex() == 1:
            shown = getattr(self, "_profile_row", {}).get("name")
            # The merged list, so the profile shows the same HRM name and status as the table.
            current = next((row for row in self.directory_rows if row.get("name") == shown), None)
            if current is not None:
                self._populate_profile(current, refresh_photos=refresh_profile_photos)

    def _render_page(self, rows):
        # Qt defers deleting replaced cell widgets; hide old chips/actions now
        # so an asynchronous review refresh cannot briefly paint both versions.
        for index in range(self.employee_table.rowCount()):
            for column in (3,5):
                previous = self.employee_table.cellWidget(index,column)
                if previous is not None:
                    previous.hide()
        self._page_rows = list(rows)
        self.employee_table.setUpdatesEnabled(False)
        self.employee_table.clearSelection()
        self.employee_table.setRowCount(len(self._page_rows))
        for index, row in enumerate(self._page_rows):
            self._set_photo_cell(index, row)
            org = " · ".join(part for part in (row.get("department"), row.get("position")) if part)
            name_text = row["employee_name"] + ("\n" + org if org else "")
            details = [row["employee_name"]]
            for label, key in (("Department", "department"), ("Position", "position"),
                               ("Location", "location"), ("Cost center", "cost_center"),
                               ("Employee type", "employee_type")):
                if row.get(key):
                    details.append(f"{label}: {row[key]}")
            if row.get("in_hrm"):
                details.append(f"HRM status: {str(row.get('hrm_status') or '').title()}")
            elif self._hrm_directory.synced:
                details.append("Not listed as active in HRM")
            for col, value, tip in ((1, name_text, "\n".join(details)), (2, row["employee_id"], row["employee_id"]),
                                    (4, str(hd.face_count(row)), str(hd.face_count(row)))):
                item = QTableWidgetItem(value)
                item.setToolTip(tip)
                if col == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.employee_table.setItem(index, col, item)
            if not row.get("enrolled", True):
                self._render_hrm_only_row(index, row)
                continue
            status = QWidget()
            status_layout = QHBoxLayout(status)
            status_layout.setContentsMargins(8, 0, 8, 0)
            duplicates = self._duplicates_by_label.get(row["name"], [])
            hrm_left = row.get("in_hrm") and row.get("hrm_status") not in ("", "ACTIVE")
            pill = StatusPill("Inactive" if not row.get("active", True) else
                              "Not in HRM" if row.get("hrm_blocked") else
                              f"HRM {str(row['hrm_status']).title()}" if hrm_left else
                              "Review match" if duplicates else
                              ("Active" if hd.face_count(row) else "Needs enrollment"),
                              "bad" if not row.get("active", True) or row.get("hrm_blocked") else
                              "warn" if hrm_left or duplicates or not hd.face_count(row) else "ok")
            if row.get("hrm_blocked"):
                pill.setToolTip(NOT_IN_HRM_TIP)
            if hrm_left:
                pill.setToolTip("HRM lists this employee as " + str(row["hrm_status"]).lower()
                                + ". Use Mark inactive to block their attendance here.")
            if duplicates:
                explanation = ("Possible duplicate face with " + "; ".join(owner for _, owner in duplicates)
                               + ". Review both profiles before making changes. No records were changed.")
                pill.setToolTip(explanation)
                pill.setAccessibleName(explanation)
                status.setToolTip(explanation)
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
            menu_btn = QPushButton("Edit")
            menu_btn.setObjectName("controlButton")
            menu_btn.setProperty("overflowMenu", True)
            menu_btn.setToolTip(f"Actions: {row['employee_name']}")
            apply_button_icon(menu_btn, "more", palette(self._theme)["text_secondary"])
            menu = QMenu(menu_btn)
            for label, cb, icon_name in (
                ("Edit name", self._edit_selected, "user"),
                ("Update photo", self._use_selected, "camera"),
                ("View profile", self._show_profile, "user"),
            ):
                act = menu.addAction(label)
                act.setIcon(make_icon(icon_name, 16, palette(self._theme)["text_secondary"]))
                act.triggered.connect(lambda checked=False, i=index, fn=cb: self._row_action(i, fn))
            menu.addSeparator()
            for counterpart, owner in duplicates:
                review = menu.addAction(f"Review match: {owner}")
                review.triggered.connect(lambda checked=False, label=counterpart: self._show_duplicate_profile(label))
            if duplicates:
                menu.addSeparator()
            toggle = menu.addAction("Mark inactive (terminated)" if row.get("active", True) else "Reactivate employee")
            toggle.triggered.connect(lambda checked=False, i=index: self._row_action(i, self._toggle_employee_active))
            del_act = menu.addAction("Delete employee")
            del_act.setIcon(make_icon("trash", 16, palette(self._theme)["danger"]))
            del_act.triggered.connect(lambda checked=False, i=index: self._row_action(i, self._remove_employee))
            menu_btn.setMenu(menu)
            action_layout.addWidget(menu_btn)
            action_layout.addStretch()
            self.employee_table.setCellWidget(index, 5, actions)
            self.employee_table.setRowHeight(index, 64)
        self.employee_table.setUpdatesEnabled(True)
        self._request_hrm_photos(self._page_rows)

    def _set_photo_cell(self, index, row):
        photo = self._find_enrollment_photo(row["name"], row["employee_name"]) if row.get("name") else None
        tip = "Saved enrollment photo" if photo else "No saved enrollment photo"
        if photo is None and row.get("in_hrm"):
            photo = hd.cached_photo(self.settings, row["employee_id"])
            if photo is not None:
                tip = "HRM profile photo"
        pixmap = self._thumbnail_for(photo, 48) if photo else None
        if pixmap is None or pixmap.isNull():
            initials = "".join(word[0] for word in row["employee_name"].split()[:2]) or "?"
            pixmap = _placeholder_pixmap(48, initials, self._theme)
        image_item = QTableWidgetItem()
        image_item.setIcon(QIcon(pixmap))
        image_item.setToolTip(tip)
        self.employee_table.setItem(index, 0, image_item)

    def _render_hrm_only_row(self, index, row):
        """An HRM employee with no face enrolled yet: one action, Enroll face."""
        status = QWidget()
        status.setObjectName("employeeCell")
        status.setStyleSheet("QWidget#employeeCell { background: transparent; }")
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(8, 0, 8, 0)
        active = row.get("hrm_status") == "ACTIVE"
        ready = active and hd.face_count(row) > 0
        pill = StatusPill("Active" if ready else "Not enrolled" if active
                          else f"HRM {str(row.get('hrm_status') or '').title()}",
                          "ok" if ready else "warn" if active else "idle")
        pill.setToolTip("The Live Monitor recognizes this employee from their enrolled angles." if ready else
                        "In HRM, no angles enrolled yet. Generate them in AI Face Angles." if active else
                        "HRM lists this employee as " + str(row.get("hrm_status") or "").lower() + ".")
        pill.setFixedHeight(28)
        status_layout.addWidget(pill)
        status_layout.addStretch()
        self.employee_table.setCellWidget(index, 3, status)
        actions = QWidget()
        actions.setObjectName("employeeCell")
        actions.setStyleSheet("QWidget#employeeCell { background: transparent; }")
        action_layout = QHBoxLayout(actions)
        action_layout.setContentsMargins(4, 8, 4, 8)
        enroll = QPushButton("Enroll")
        enroll.setObjectName("controlButton")
        enroll.setToolTip(f"Enroll a face for {row['employee_name']} ({row['employee_id']})")
        apply_button_icon(enroll, "camera", palette(self._theme)["text_secondary"])
        enroll.clicked.connect(lambda checked=False, i=index: self._row_action(i, self._enroll_selected_from_hrm))
        action_layout.addWidget(enroll)
        action_layout.addStretch()
        self.employee_table.setCellWidget(index, 5, actions)
        self.employee_table.setRowHeight(index, 64)

    def _activate_selected(self):
        row = self.selected_row()
        if row is None:
            return
        if row.get("enrolled", True):
            self._show_profile()
        else:
            self._enroll_selected_from_hrm()

    def _enroll_selected_from_hrm(self):
        """Enroll someone HRM lists: as angles when matching on them, else as local samples."""
        row = self.selected_row()
        if row is None or row.get("enrolled", True):
            return
        if self._angle_counts() is not None:
            # Local samples aren't used for matching now, so they would never recognize this person.
            self.anglesRequested.emit(row["employee_id"])
            return
        self._open_enrollment()
        self.names.set_values(row.get("first_name", ""), row.get("last_name", ""), row["employee_name"])
        self.id_edit.setText(row["employee_id"])
        self.id_edit.setReadOnly(True)
        self.wizard_card.set_subtitle(f"Enroll {row['employee_name']} from HRM. Capture or upload a clear face photo.")

    def select_row(self, row):
        """Show the page holding *row* and select it. False if the filters hide it."""
        if row not in self.filtered_rows:
            return False
        position = self.filtered_rows.index(row)
        self._go_to_page(position // self._page_size())
        self.employee_table.selectRow(position % self._page_size())
        return True

    # --- HRM directory -------------------------------------------------------

    def _hrm_config(self):
        return config_from_settings(self.settings)

    def _auto_sync_hrm(self):
        config = self._hrm_config()
        if not config.configured:
            return
        if self._hrm_directory.outdated or time.time() - self._hrm_directory.fetched_at > HRM_AUTO_SYNC_SEC:
            self.sync_hrm(quiet=True)

    def request_hrm_refresh(self):
        """HRM says something changed. Sync now, or once more if a sync is already running,
        because that sync may have read the employee before the change."""
        if self._hrm_syncing:
            self._hrm_sync_again = True
            return
        self.sync_hrm(quiet=True)

    def set_hrm_live(self, connected):
        """Show whether HRM changes are arriving instantly."""
        self._hrm_live = bool(connected)
        self._update_hrm_status()

    def sync_hrm(self, quiet=False):
        """Download every HRM employee on a worker thread; the table updates when it's done."""
        if self._hrm_syncing:
            return False
        config = self._hrm_config()
        if not config.configured:
            if not quiet:
                problem = hd.hrm_sync.configuration_problem(config)
                self._notify(problem.replace("send angles to HRM", "load employees from HRM"), "warn")
            return False
        self._hrm_syncing = True
        self._hrm_quiet = quiet
        self.hrm_sync_button.setEnabled(False)
        self.hrm_sync_button.setText("Syncing…")
        self._update_hrm_status()
        path = hd.cache_path(self.settings)

        def work():
            try:
                result = hd.sync(config, path, progress=lambda done, total: self.hrmSyncProgress.emit(done, total))
            except HRMError as exc:
                result = exc
            except Exception as exc:  # Never let a worker die silently.
                result = HRMError(f"Could not load employees from HRM: {exc}")
            try:
                self.hrmSyncDone.emit(result)
            except RuntimeError:
                pass  # The screen was closed while syncing.

        threading.Thread(target=work, name="hrm-directory-sync", daemon=True).start()
        return True

    def _on_hrm_progress(self, done, total):
        if self._hrm_syncing:
            self.hrm_status_label.setText(f"Loading employees from HRM… {done} of {total}")

    def _on_hrm_synced(self, result):
        self._hrm_syncing = False
        self.hrm_sync_button.setEnabled(True)
        self.hrm_sync_button.setText("Sync from HRM")
        again, self._hrm_sync_again = self._hrm_sync_again, False
        if isinstance(result, Exception):
            self._hrm_sync_error = str(result)
            self._update_hrm_status()
            if not getattr(self, "_hrm_quiet", False):
                self._notify(str(result), "bad")
            if again:
                self.sync_hrm(quiet=True)
            return
        self._hrm_sync_error = ""
        self._hrm_directory = result
        self._hrm_photo_missing.clear()
        self.reload(refresh_profile_photos=False)
        # The Live Monitor takes the new names and statuses now, not on its next file check.
        try:
            self.engine.reload_if_directory_changed()
        except Exception as exc:
            logging.exception("Could not apply the HRM sync to the Live Monitor")
            self._notify(f"Could not update the Live Monitor: {exc}", "bad")
        if again:
            self.sync_hrm(quiet=True)
        if not getattr(self, "_hrm_quiet", False):
            missing = sum(1 for row in self.directory_rows if row.get("in_hrm") and not row.get("enrolled", True)
                          and row.get("hrm_status") == "ACTIVE")
            self._notify(f"Loaded {len(result.employees)} employees from HRM. "
                         f"{missing} active employees still need a face enrolled.", "ok")

    def _update_hrm_status(self):
        directory = self._hrm_directory
        if self._hrm_syncing:
            text = "Loading employees from HRM…"
        elif not self._hrm_config().configured and not directory.synced:
            text = ("Showing local enrollments only. Add the HRM address and API key in "
                    "Settings > AI Configuration, then click Sync from HRM to list every employee.")
        elif not directory.synced:
            text = "Not synced with HRM yet. Click Sync from HRM to list every employee."
        else:
            in_hrm = len(directory.employees)
            local_only = sum(1 for row in self.directory_rows if not row.get("in_hrm"))
            text = (f"{in_hrm} active employees from HRM, synced {directory.age_text()}."
                    + (" Live updates on." if self._hrm_live else "")
                    + (f" {local_only} local enrollments are not active in HRM." if local_only else ""))
        if self._hrm_sync_error and not self._hrm_syncing:
            text += f" Last sync failed: {self._hrm_sync_error}"
        self.hrm_status_label.setText(text)

    def _request_hrm_photos(self, rows):
        """Fetch HRM photos for rows on screen that show initials, a few at a time, in the background."""
        config = self._hrm_config()
        if not config.configured:
            return
        wanted = []
        for row in rows:
            code = row.get("employee_id")
            if (not row.get("in_hrm") or not row.get("has_photo") or code in self._hrm_photo_missing
                    or (row.get("name") and self._find_enrollment_photo(row["name"], row["employee_name"]))
                    or hd.cached_photo(self.settings, code) is not None):
                continue
            with self._hrm_photo_lock:
                if code in self._hrm_photo_pending:
                    continue
                self._hrm_photo_pending.add(code)
            wanted.append(code)
        if not wanted:
            return
        settings = self.settings
        lock, pending, missing = self._hrm_photo_lock, self._hrm_photo_pending, self._hrm_photo_missing
        queue = list(wanted)

        def work():
            while True:
                with lock:
                    if not queue:
                        return
                    code = queue.pop(0)
                try:
                    path = hd.fetch_photo(config, settings, code)
                except (HRMError, OSError):
                    path = None
                with lock:
                    pending.discard(code)
                    if path is None:
                        missing.add(code)
                if path is not None:
                    try:
                        self.hrmPhotoReady.emit(code)
                    except RuntimeError:
                        return

        for number in range(min(HRM_PHOTO_WORKERS, len(wanted))):
            threading.Thread(target=work, name=f"hrm-photo-{number}", daemon=True).start()

    def _on_hrm_photo(self, code):
        # The saved file keeps its name when the photo changes, so its old thumbnail must go.
        path = str(hd.photo_dir(self.settings) / hd._photo_name(code))
        self._thumb_cache = {key: pixmap for key, pixmap in self._thumb_cache.items() if key[0] != path}
        self._hrm_photo_missing.discard(code)
        for index, row in enumerate(self._page_rows):
            if row.get("employee_id") == code and index < self.employee_table.rowCount():
                self._set_photo_cell(index, row)

    def _row_action(self, index, action):
        self.employee_table.selectRow(index)
        action()

    def selected_row(self):
        selected = self.employee_table.selectionModel().selectedRows()
        index = selected[0].row() if selected else -1
        return self._page_rows[index] if 0 <= index < len(self._page_rows) else None

    def _edit_selected(self):
        row = self.selected_row()
        if row is None:
            return
        if row.get("in_hrm"):
            self._notify("This name comes from SV HRM. Change it there and it updates here "
                         "and on the Live Monitor automatically.", "info")
            return
        names = EditNameDialog.ask(row, self)
        if names is None:
            return
        first_name, last_name, full_name = names
        try:
            self.service.update_employee(row["name"], full_name, first_name, last_name)
        except (ValueError, OSError) as exc:
            self._notify(str(exc), "bad")
            return
        self.reload()
        self.engine.reload_catalog()
        self._notify("Employee updated. Employee ID and attendance history are unchanged.", "ok")

    def _toggle_employee_active(self):
        if not guard(self, "manage_employees", "change employment status"):
            return
        row = self.selected_row()
        if row is None:
            return
        active = not row.get("active", True)
        try:
            employee_id = self.service.set_employee_active(row["name"], active)
            self.engine.persistence.log_audit("employee_status_changed",
                f"{employee_id}: {'active' if active else 'inactive'}")
            self.engine.reload_catalog()
        except (ValueError, OSError, RuntimeError) as exc:
            self._notify(str(exc), "bad")
            self.reload()
            return
        self.reload()
        self._notify("Employee reactivated." if active else
                     "Employee inactive. Attendance blocked; face templates retained for detection.", "ok")

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

    # --- moving employees between computers ----------------------------------
    def _export_employees(self):
        if not guard(self, "export_data", "export employees"):
            return
        desktop = Path.home() / "Desktop"
        default = (desktop if desktop.is_dir() else Path.home()) / \
            f"SV-Face-ID-employees-{datetime.now():%Y-%m-%d}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Export employees", str(default), EXPORT_FILTER)
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        try:
            employees, samples = export_employees(path, self.settings.encodings_path,
                                                  self.settings.employees_path)
        except (TransferError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "Couldn't export employees", str(exc))
            return
        self._notify(f"Exported {employees} employee(s) and {samples} face sample(s) to "
                     f"{Path(path).name}. Keep it private: it contains face photos.", "ok")

    def _import_employees(self):
        if not guard(self, "manage_employees", "import employees"):
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import employees", str(Path.home()), EXPORT_FILTER)
        if not path:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = import_employees(path, self.settings.encodings_path, self.settings.employees_path,
                                      duplicate_distance=self.settings.face_tolerance)
        except (TransferError, OSError, ValueError) as exc:
            report, error = None, str(exc)
        finally:
            QApplication.restoreOverrideCursor()
        if report is None:
            QMessageBox.warning(self, "Couldn't import employees", error)
            return
        if report.changed:
            self.engine.reload_catalog()
        if report.skipped:
            QMessageBox.warning(self, "Imported with some skipped", report.summary())
        else:
            self._notify(report.summary(), "ok" if report.changed else "info")

    # =======================================================================
    #  Settings / lifecycle
    # =======================================================================

    def on_settings_changed(self, settings):
        self._invalidate_checks()
        self.settings = settings
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self.service = EnrollmentService(settings.encodings_path, settings.employees_path,
                                         duplicate_distance=settings.face_tolerance)
        self.runner.service = self.service
        self.sample_runner.service = self.service
        if self.preview_on:
            self._stop_preview()
        self.reload()

    def closeEvent(self, event):
        self._duplicates_closed = True
        self._invalidate_checks()
        self.editor.hide()
        self.probe_timer.stop()
        self.countdown_timer.stop()
        self.preview_timer.stop()
        if self.preview_on:
            self._stop_preview(resume=False)
        for signal in (self.probeReady, self.duplicatesReady, self.runner.finished, self.sample_runner.validated):
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
