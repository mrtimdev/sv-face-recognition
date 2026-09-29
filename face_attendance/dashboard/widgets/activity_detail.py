"""Attendance evidence viewer with a frozen, blurred window backdrop."""
import hashlib
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QImageReader, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (QDialog, QFrame, QGraphicsBlurEffect, QHBoxLayout,
                             QLabel, QPushButton, QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

from ..icons import apply_button_icon
from ..theme import palette
from ...enrollment_photos import sample_photo_map


def load_photo(path, limit=1200):
    """Decode at display resolution rather than retain full camera originals."""
    if not path:
        return QPixmap()
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > limit:
        reader.setScaledSize(size.scaled(limit, limit, Qt.AspectRatioMode.KeepAspectRatio))
    return QPixmap.fromImage(reader.read())


def enrollment_photos(settings, employee_id, rows):
    """Resolve exact catalog labels for this ID, including safe legacy photos."""
    if not employee_id:
        return []
    directory = Path(settings.encodings_path).parent / "enrollment_photos"
    safe = lambda label: "".join(c if c.isalnum() or c in "-_" else "_" for c in str(label))[:60]
    photos = []
    saved = sample_photo_map(settings.encodings_path)
    for row in rows:
        if str(row["employee_id"]) != str(employee_id):
            continue
        label = row["name"]
        saved_samples = [path for path in saved.get(label, [])
                         if path is not None and path.is_file()]
        if saved_samples:
            photos.extend(path for path in saved_samples if path not in photos)
            continue
        digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
        path = directory / f"{safe(label)}_{digest}.jpg"
        if not path.is_file():
            collisions = sum(safe(other["name"]).casefold() == safe(label).casefold() for other in rows)
            path = directory / f"{safe(label)}.jpg"
            if collisions != 1 or not path.is_file():
                continue
        if path not in photos:
            photos.append(path)
    return photos


class EvidencePhoto(QWidget):
    """Rounded photo surface, preserving the entire image in the detail view."""
    def __init__(self, pixmap=None, theme="light", thumbnail=False, parent=None):
        super().__init__(parent)
        self._pixmap = pixmap if pixmap is not None else QPixmap()
        self._theme = theme
        self._thumbnail = thumbnail
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(40, 40)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_photo(self, pixmap):
        self._pixmap = pixmap
        self.update()

    def set_theme(self, theme):
        self._theme = theme
        self.update()

    def paintEvent(self, event):
        c = palette(self._theme)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 12, 12)
        p.setClipPath(path)
        p.fillRect(self.rect(), QColor(c["bg_soft"]))
        if not self._pixmap.isNull():
            photo = self._pixmap.scaled(self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding if self._thumbnail else Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap((self.width() - photo.width()) // 2, (self.height() - photo.height()) // 2, photo)
        else:
            p.setPen(QColor(c["text_secondary"]))
            p.drawText(self.rect().adjusted(12, 12, -12, -12),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                       "—" if self._thumbnail else "Image unavailable")
        p.end()


class ActivityDetailDialog(QDialog):
    def __init__(self, entry, photo_paths, theme="light", parent=None):
        super().__init__(parent)
        self.entry = entry
        self.photo_paths = list(photo_paths)
        self._photo_index = 0
        self._theme = theme
        self.setWindowTitle("Attendance detail")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Blur a snapshot, keeping the recognition engine and live video untouched.
        self.backdrop = QLabel(self)
        self.backdrop.setPixmap(parent.grab())
        self.backdrop.setScaledContents(True)
        blur = QGraphicsBlurEffect(self.backdrop)
        blur.setBlurRadius(18)
        self.backdrop.setGraphicsEffect(blur)
        self.scrim = QWidget(self)
        self.scrim.setObjectName("evidenceScrim")
        self.scrim.setStyleSheet("QWidget#evidenceScrim { background: rgba(4, 12, 26, 150); }")
        self._build()
        parent.installEventFilter(self)
        self._fit_parent()

    def _label(self, text, name="evidenceBody"):
        label = QLabel(str(text))
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        return label

    def _build(self):
        c = palette(self._theme)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)
        self.card = QFrame()
        self.card.setObjectName("evidenceCard")
        self.card.setMaximumWidth(960)
        self.card.setStyleSheet(f"""
            QFrame#evidenceCard {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 24px; }}
            QWidget#evidenceContent, QScrollArea#evidenceScroll {{ background: transparent; border: none; }}
            QLabel {{ background: transparent; border: none; }}
            QLabel#evidenceEyebrow {{ color: {c['primary_soft_fg']}; font-size: 10px; font-weight: 700; letter-spacing: 2px; }}
            QLabel#evidenceTitle {{ color: {c['text']}; font-size: 26px; font-weight: 700; }}
            QLabel#evidenceBody {{ color: {c['text_secondary']}; font-size: 12px; }}
            QLabel#evidenceSection {{ color: {c['text']}; font-size: 14px; font-weight: 600; }}
            QFrame#evidenceFacts {{ background: {c['bg_soft']}; border: none; border-radius: 14px; }}
            QPushButton {{ padding: 8px 14px; border-radius: 10px; }}
        """)
        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(6)
        titles.addWidget(self._label("ATTENDANCE / CAPTURE DETAIL", "evidenceEyebrow"))
        titles.addWidget(self._label(self.entry["name"], "evidenceTitle"))
        titles.addWidget(self._label(f"Employee ID  {self.entry.get('employee_id') or '—'}"))
        header.addLayout(titles, 1)
        close = QPushButton()
        close.setObjectName("iconButton")
        close.setFixedSize(36, 36)
        close.setAccessibleName("Close attendance detail")
        close.setToolTip("Close (Esc)")
        apply_button_icon(close, "x", c["text_secondary"])
        close.clicked.connect(self.reject)
        header.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("evidenceScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumHeight(180)
        content = QWidget()
        content.setObjectName("evidenceContent")
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(18)
        facts = QFrame()
        facts.setObjectName("evidenceFacts")
        fact_row = QHBoxLayout(facts)
        fact_row.setContentsMargins(16, 12, 16, 12)
        for title, value in (("RESULT", self.entry["status"]), ("CAPTURED", self.entry.get("captured_at", "—")),
                             ("VERIFIED PRESENCE", self.entry.get("duration", "—"))):
            column = QVBoxLayout()
            column.addWidget(self._label(title, "evidenceEyebrow"))
            column.addWidget(self._label(value, "evidenceSection"))
            fact_row.addLayout(column, 1)
        body.addWidget(facts)
        photos = QHBoxLayout()
        photos.setSpacing(18)
        capture_col = QVBoxLayout()
        capture_header = QHBoxLayout()
        self.capture_title = self._label("01   Attendance capture", "evidenceSection")
        self.capture_title.setMinimumHeight(38)
        capture_header.addWidget(self.capture_title, 1)
        self.context_button = QPushButton("Full frame")
        self.context_button.setCheckable(True)
        self.context_button.setAccessibleName("Toggle face crop and original full frame")
        self.context_button.clicked.connect(self._toggle_context)
        fallback = self.entry.get("context_capture")
        self.context_button.setVisible(bool(self.entry.get("context_path")) or
                                       (fallback is not None and not fallback.isNull()))
        capture_header.addWidget(self.context_button)
        capture_col.addLayout(capture_header)
        self.capture_photo = EvidencePhoto(self.entry.get("capture"), self._theme)
        self.capture_photo.setMinimumHeight(260)
        capture_col.addWidget(self.capture_photo, 1)
        self.capture_caption = self._label("Verified face crop from this event" if self.entry.get("face_box")
                                           else "Image from this attendance event")
        self.capture_caption.setMinimumHeight(38)
        capture_col.addWidget(self.capture_caption)
        photos.addLayout(capture_col, 1)
        enrolled_col = QVBoxLayout()
        enrolled_title = self._label("02   Enrolled images", "evidenceSection")
        enrolled_title.setMinimumHeight(38)
        enrolled_col.addWidget(enrolled_title)
        self.enrolled_photo = EvidencePhoto(theme=self._theme)
        self.enrolled_photo.setMinimumHeight(260)
        enrolled_col.addWidget(self.enrolled_photo, 1)
        navigation = QHBoxLayout()
        self.photo_count = self._label("")
        navigation.addWidget(self.photo_count, 1)
        self.previous = QPushButton()
        self.next = QPushButton()
        for button, icon, title, step in ((self.previous, "chevron-left", "Previous enrolled image", -1),
                                           (self.next, "chevron-right", "Next enrolled image", 1)):
            button.setFixedSize(32, 28)
            button.setAccessibleName(title)
            button.setToolTip(title)
            apply_button_icon(button, icon, c["text_secondary"])
            button.clicked.connect(lambda _checked=False, delta=step: self._navigate(delta))
            navigation.addWidget(button)
        enrolled_col.addLayout(navigation)
        photos.addLayout(enrolled_col, 1)
        body.addLayout(photos)
        if self.entry.get("detail"):
            body.addWidget(self._label(self.entry["detail"]))
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        footer = QHBoxLayout()
        footer.addWidget(self._label("Capture and enrollment reference"), 1)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.clicked.connect(self.accept)
        footer.addWidget(done)
        layout.addLayout(footer)
        outer.addWidget(self.card, 1, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        self._navigate(0)

    def _toggle_context(self, checked):
        if checked:
            photo = load_photo(self.entry.get("context_path"))
            if photo.isNull():
                photo = self.entry.get("context_capture")
            if photo is None or photo.isNull():
                self.context_button.setChecked(False)
                self.capture_caption.setText("Full-frame image unavailable")
                return
            self.capture_photo.set_photo(photo)
            self.capture_title.setText("01   Full-frame evidence")
            self.capture_caption.setText("Original camera frame from this event")
            self.context_button.setText("Face crop")
        else:
            self.capture_photo.set_photo(self.entry["capture"])
            self.capture_title.setText("01   Attendance capture")
            self.capture_caption.setText("Verified face crop from this event")
            self.context_button.setText("Full frame")

    def _navigate(self, delta):
        if self.photo_paths:
            self._photo_index = min(max(0, self._photo_index + delta), len(self.photo_paths) - 1)
            self.enrolled_photo.set_photo(load_photo(self.photo_paths[self._photo_index]))
            self.photo_count.setText(f"{self._photo_index + 1} / {len(self.photo_paths)} enrolled images")
        else:
            self.photo_count.setText("No enrollment photo saved")
        self.previous.setEnabled(self._photo_index > 0)
        self.next.setEnabled(self._photo_index + 1 < len(self.photo_paths))

    def _fit_parent(self):
        parent = self.parentWidget()
        self.setGeometry(QRect(parent.mapToGlobal(QPoint(0, 0)), parent.size()))
        self.card.setFixedSize(min(960, max(280, self.width() - 48)), min(680, max(240, self.height() - 48)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for name in ("backdrop", "scrim"):
            layer = getattr(self, name, None)
            if layer is not None:
                layer.setGeometry(self.rect())

    def eventFilter(self, watched, event):
        if watched is self.parentWidget():
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
                self._fit_parent()
            elif event.type() == QEvent.Type.Close:
                self.reject()
        return super().eventFilter(watched, event)

    def done(self, result):
        self.parentWidget().removeEventFilter(self)
        super().done(result)

    def mousePressEvent(self, event):
        if not self.card.geometry().contains(event.pos()):
            self.reject()
        else:
            super().mousePressEvent(event)
