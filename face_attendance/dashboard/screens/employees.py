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
from PyQt6.QtCore import QObject, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMessageBox, QPushButton, QScrollArea, QSpinBox, QStackedWidget,
                             QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...camera import CameraManager
from ...enrollment import EnrollmentOutcome, EnrollmentService, FACE_BACKEND_LOCK
from ..bridge import to_pixmap
from ..theme import palette
from ..widgets import StatCard, StatusPill, ToastBar, VideoView


PHOTO_FILTER = "Images (*.jpg *.jpeg *.png *.bmp *.tiff)"
TABLE_HEADERS = ("", "Label", "Employee ID", "Display Name", "Samples", "Status")
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
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self._build()
        self._connect()
        self.reload()

    # =======================================================================
    #  Layout
    # =======================================================================

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # --- header ---
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

        # --- stat cards ---
        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.stat_enrolled = StatCard("Enrolled", "0", "Unique employee IDs")
        self.stat_samples = StatCard("Total Samples", "0", "Face encodings stored")
        self.stat_captures = StatCard("Evidence Captures", "0", "Auto-saved snapshots")
        self.stat_latest = StatCard("Latest Enrollment", "-", "Most recent save")
        for card in (self.stat_enrolled, self.stat_samples, self.stat_captures, self.stat_latest):
            stats.addWidget(card)
        layout.addLayout(stats)

        # --- body ---
        body = QHBoxLayout()
        body.setSpacing(12)

        # left: table + actions
        left = QVBoxLayout()
        left.setSpacing(8)
        self.table = QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, THUMB_SIZE + 16)
        for col in range(1, len(TABLE_HEADERS)):
            header_view.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.cellClicked.connect(self._on_cell_clicked)
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
        self.notes = QLabel("Removing enrollment never deletes attendance history: the rows "
                            "keep their recorded name and employee ID.")
        self.notes.setObjectName("screenSubtitle")
        self.notes.setWordWrap(True)
        left.addWidget(self.notes)
        body.addLayout(left, 3)

        # right: stacked (wizard | profile)
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self._build_wizard())       # index 0
        self.right_stack.addWidget(self._build_profile_card())  # index 1
        body.addWidget(self.right_stack, 2)
        layout.addLayout(body, 1)

        self.toast = ToastBar()
        layout.addWidget(self.toast)

    # -----------------------------------------------------------------------
    #  Enrollment wizard (kept intact, just wrapped)
    # -----------------------------------------------------------------------

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

    # -----------------------------------------------------------------------
    #  Employee profile card (new)
    # -----------------------------------------------------------------------

    def _build_profile_card(self):
        card = QFrame()
        card.setObjectName("panel")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(12)

        # -- heading --
        heading = QLabel("Employee Profile")
        heading.setObjectName("sectionTitle")
        outer.addWidget(heading)

        # -- photo + info row --
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
        outer.addLayout(header)

        # -- gallery section --
        gallery_header = QHBoxLayout()
        gallery_title = QLabel("Evidence captures")
        gallery_title.setObjectName("sectionTitle")
        self.gallery_count_label = QLabel("")
        self.gallery_count_label.setObjectName("screenSubtitle")
        gallery_header.addWidget(gallery_title)
        gallery_header.addStretch()
        gallery_header.addWidget(self.gallery_count_label)
        outer.addLayout(gallery_header)

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
        outer.addWidget(self.gallery_scroll)

        self.gallery_hint = QLabel("Click any photo to view full size")
        self.gallery_hint.setObjectName("screenSubtitle")
        outer.addWidget(self.gallery_hint)

        # -- profile actions --
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.p_add_btn = QPushButton("Add samples")
        self.p_del_btn = QPushButton("Delete newest sample")
        self.p_rem_btn = QPushButton("Remove employee")
        self.p_rem_btn.setObjectName("danger")
        for btn in (self.p_add_btn, self.p_del_btn, self.p_rem_btn):
            actions.addWidget(btn)
        actions.addStretch()
        outer.addLayout(actions)

        self.p_open_folder_btn = QPushButton("Open captures folder")
        self.p_enroll_btn = QPushButton("Enroll new employee")
        self.p_enroll_btn.setObjectName("primary")
        row = QHBoxLayout()
        row.addWidget(self.p_open_folder_btn)
        row.addStretch()
        row.addWidget(self.p_enroll_btn)
        outer.addLayout(row)

        outer.addStretch()
        return card

    # =======================================================================
    #  Wiring
    # =======================================================================

    def _connect(self):
        self.engine.catalogChanged.connect(lambda rows: self.reload())
        self.runner.finished.connect(self.on_enrollment_finished)
        self.sample_runner.validated.connect(self.on_sample_validated)
        self.probeReady.connect(self.on_probe)

        # Timers
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

        # Profile card actions
        self.p_add_btn.clicked.connect(self._use_selected)
        self.p_del_btn.clicked.connect(self._remove_sample)
        self.p_rem_btn.clicked.connect(self._remove_employee)
        self.p_enroll_btn.clicked.connect(self._switch_to_wizard)
        self.p_open_folder_btn.clicked.connect(self._open_captures_for_selected)

    def _tab_changed(self, index):
        capture = index == 0
        if capture and self.preview_on:
            self.probe_timer.start()
        else:
            self.probe_timer.stop()
        if not capture and self.preview_on:
            self._toggle_preview()

    # =======================================================================
    #  Photo management
    # =======================================================================

    # --- enrollment photo directory ----------------------------------------

    def _enrollment_photo_dir(self):
        return Path(self.settings.encodings_path).parent / ENROLLMENT_PHOTOS_DIR

    def _enrollment_photo_path(self, label):
        """Canonical path for a saved enrollment photo by enrollment label."""
        return self._enrollment_photo_dir() / f"{_safe_name(label)}.jpg"

    def _save_enrollment_photo(self, label, samples):
        """Persist the first enrollment sample as the employee's profile photo."""
        if not samples:
            return
        photo_dir = self._enrollment_photo_dir()
        photo_dir.mkdir(parents=True, exist_ok=True)
        path = self._enrollment_photo_path(label)
        try:
            cv2.imwrite(str(path), samples[0], [cv2.IMWRITE_JPEG_QUALITY, 92])
        except Exception:
            pass  # Non-critical; the table falls back to a placeholder.

    def _find_enrollment_photo(self, label, display_name):
        """Return the best enrollment photo for an employee, or None.

        Priority:
        1. ``enrollment_photos/{safe_label}.jpg`` — saved by the dashboard.
        2. ``faces/`` directory — manual enrollment photos, matched by common
           name transformations (e.g. "Mr A" matches ``mr-a.jpg``).
        """
        # 1. Saved enrollment photo
        path = self._enrollment_photo_path(label)
        if path.exists():
            return path

        # 2. Look in the faces/ directory with fuzzy matching
        faces_dir = Path(self.settings.encodings_path).parent / "faces"
        if faces_dir.is_dir():
            candidates = set()
            for name in (label, display_name):
                lower = name.lower()
                candidates.add(lower.replace(" ", "-"))
                candidates.add(lower.replace(" ", "_"))
                candidates.add(lower.replace(" ", ""))
                candidates.add(_safe_name(name).lower())
                # Strip common honorific prefixes ("Mr Tim Dev" -> "tim-dev")
                for prefix in ("mr ", "mrs ", "ms ", "dr ", "mr. ", "mrs. ", "ms. ", "dr. "):
                    if lower.startswith(prefix):
                        stripped = lower[len(prefix):]
                        candidates.add(stripped.replace(" ", "-"))
                        candidates.add(stripped.replace(" ", "_"))
                        candidates.add(stripped.replace(" ", ""))
                        break
            for candidate in candidates:
                for ext in IMAGE_EXTENSIONS:
                    guess = faces_dir / f"{candidate}{ext}"
                    if guess.exists():
                        return guess

        return None

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
            self.toast.show_message("Image file not found on disk.", "warn")

    # =======================================================================
    #  Profile card population
    # =======================================================================

    def _switch_to_wizard(self):
        self.table.clearSelection()
        self.right_stack.setCurrentIndex(0)

    def _switch_to_profile(self):
        self.right_stack.setCurrentIndex(1)

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
            "MAPPED" if row["mapped"] else "DERIVED",
            "ok" if row["mapped"] else "warn")
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
            self.toast.show_message(f"Camera settings are invalid: {exc}", "bad")
            return
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
            with FACE_BACKEND_LOCK:
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
        # Keep a reference so we can save an enrollment photo after success.
        self._enrollment_save_samples = [s.copy() for s in self.pending]
        self.save_button.setEnabled(False)
        self.save_button.setText("Saving...")
        self.toast.show_message("Encoding samples...", "info")
        self.runner.submit(self.pending, name, self.id_edit.text().strip(),
                           self.quality_check.isChecked())

    def on_enrollment_finished(self, outcome):
        self.save_button.setText("Save enrollment")
        self.save_button.setEnabled(bool(self.pending))
        if not outcome.ok:
            self._enrollment_save_samples = []
            self.toast.show_message(f"{outcome.message}", "bad")
            return
        # Save the first sample as the enrollment profile photo.
        self._save_enrollment_photo(
            outcome.name, getattr(self, "_enrollment_save_samples", []))
        self._enrollment_save_samples = []
        self.pending.clear()
        self._refresh_samples()
        self.toast.show_message(f"{outcome.message}. Total samples: {outcome.total_samples}", "ok")
        self.name_edit.clear()
        self.id_edit.clear()
        self.reload()
        self.engine.reload_catalog()

    # =======================================================================
    #  Catalog management (table, selection, delete)
    # =======================================================================

    def reload(self):
        self._captures_map = self._scan_captures()
        self._thumb_cache.clear()
        self.rows = self.service.employees()
        self.table.setRowCount(len(self.rows))
        self.table.setSortingEnabled(False)

        total_captures = sum(len(v) for v in self._captures_map.values())

        for index, row in enumerate(self.rows):
            self.table.setRowHeight(index, THUMB_SIZE + 14)

            # Photo column — show the enrollment source image
            enrollment_photo = self._find_enrollment_photo(
                row["name"], row["employee_name"])
            photo_item = QTableWidgetItem()
            if enrollment_photo:
                thumb = self._thumbnail_for(enrollment_photo, THUMB_SIZE)
                if thumb and not thumb.isNull():
                    photo_item.setIcon(QIcon(thumb))
                else:
                    photo_item.setIcon(QIcon(
                        _placeholder_pixmap(THUMB_SIZE, row["employee_name"], self._theme)))
                photo_item.setToolTip(
                    f"Enrollment photo: {enrollment_photo.name}\nClick to view")
            else:
                photo_item.setIcon(QIcon(
                    _placeholder_pixmap(THUMB_SIZE, row["employee_name"], self._theme)))
                photo_item.setToolTip("No enrollment photo yet")
            self.table.setItem(index, 0, photo_item)

            # Data columns
            values = (row["name"], row["employee_id"], row["employee_name"],
                      str(row["samples"]),
                      "Mapped" if row["mapped"] else "Derived")
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(str(value))
                if column == 5:
                    colors = palette(self._theme)
                    item.setForeground(QColor(
                        colors["accent"] if row["mapped"] else colors["warn"]))
                self.table.setItem(index, column, item)

        # Update stat cards
        total_samples = sum(row["samples"] for row in self.rows)
        people = len({row["employee_id"] for row in self.rows})
        self.stat_enrolled.set_value(people, "ok" if people else "idle")
        self.stat_samples.set_value(total_samples)
        self.stat_captures.set_value(total_captures)
        if self.rows:
            latest = max(self.rows, key=lambda r: r["samples"])
            self.stat_latest.set_value(latest["employee_name"])
            self.stat_latest.set_hint(f"{latest['samples']} sample(s)")
        else:
            self.stat_latest.set_value("-")

        self.subtitle.setText(
            f"{len(self.rows)} enrollment label(s), {people} employee ID(s), "
            f"{total_samples} sample(s) in {Path(self.settings.encodings_path).name}")
        self._selection_changed()

    def selected_row(self):
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not indexes or not 0 <= indexes[0].row() < len(self.rows):
            return None
        return self.rows[indexes[0].row()]

    def _selection_changed(self):
        row = self.selected_row()
        has_selection = row is not None
        self.use_button.setEnabled(has_selection)
        self.remove_sample_button.setEnabled(has_selection and row["samples"] > 0)
        self.remove_button.setEnabled(has_selection)
        if has_selection:
            self._populate_profile(row)
            self._switch_to_profile()
        else:
            self.right_stack.setCurrentIndex(0)

    def _on_cell_clicked(self, row_index, column):
        """Open the enrollment photo when the photo column is clicked."""
        if column != 0 or not 0 <= row_index < len(self.rows):
            return
        row = self.rows[row_index]
        photo = self._find_enrollment_photo(row["name"], row["employee_name"])
        if photo:
            self._open_image(photo)
        else:
            self.toast.show_message(
                "No enrollment photo yet. Enroll or re-enroll to save one.", "info")

    def _use_selected(self):
        row = self.selected_row()
        if row is None:
            return
        self.name_edit.setText(row["name"])
        self.id_edit.setText(row["employee_id"])
        self._switch_to_wizard()
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

    # =======================================================================
    #  Settings / lifecycle
    # =======================================================================

    def on_settings_changed(self, settings):
        self.settings = settings
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self.service = EnrollmentService(settings.encodings_path, settings.employees_path)
        self.runner.service = self.service
        if self.preview_on:
            self._stop_preview()
        self.reload()

    def closeEvent(self, event):
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
        for card in (self.stat_enrolled, self.stat_samples, self.stat_captures, self.stat_latest):
            card.set_theme(theme)
        # Refresh profile if visible
        row = self.selected_row()
        if row is not None and self.right_stack.currentIndex() == 1:
            self._populate_profile(row)
