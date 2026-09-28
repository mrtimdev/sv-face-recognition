"""Attendance Report: filtered, paged records with summaries and Excel/CSV export.

The reader connection is read-only, so this screen can never alter attendance.
"""
from datetime import date, datetime, time, timedelta
from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox,
                             QDateEdit, QDialog, QFileDialog, QFrame,
                             QGraphicsBlurEffect, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
                             QProgressBar, QPushButton, QSizePolicy, QSpinBox,
                             QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...report import (AttendanceReader, delete_records, export_excel, export_rows,
                       local_text)
from ..icons import apply_button_icon, make_icon
from ..theme import palette
from ..widgets import Card, StatCard, ToastBar

#        checkbox, image, recorded, employee, id, status, duration
COLUMNS = ("", "", "Recorded", "Employee", "ID", "Status", "Duration")
THUMB_SIZE = 40
DETAIL_IMG_SIZE = 360


def _rounded_pixmap(pixmap, size, radius=6):
    if pixmap is None or pixmap.isNull():
        return QPixmap()
    scaled = pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                           Qt.TransformationMode.SmoothTransformation)
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    p = QPainter(result)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)
    p.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    p.end()
    return result


def _placeholder_thumb(size, theme="dark"):
    c = palette(theme)
    result = QPixmap(size, size)
    result.fill(Qt.GlobalColor.transparent)
    p = QPainter(result)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), 6, 6)
    p.setClipPath(path)
    p.fillRect(0, 0, size, size, QColor(c["panel_alt"]))
    p.setPen(QColor(c["muted"]))
    font = QFont()
    font.setPixelSize(max(8, size // 3))
    p.setFont(font)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "—")
    p.end()
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  Blurred backdrop overlay
# ══════════════════════════════════════════════════════════════════════════════

class _BlurOverlay(QWidget):
    """Covers the parent with a blurred + dimmed screenshot."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setGeometry(parent.rect())
        self._snapshot = parent.grab()
        self._blurred = None
        self._prepare_blur()
        self.show()
        self.raise_()

    def _prepare_blur(self):
        src = self._snapshot.toImage()
        if src.isNull():
            return
        scale = 0.15
        small = src.scaled(
            max(1, int(src.width() * scale)),
            max(1, int(src.height() * scale)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self._blurred = QPixmap.fromImage(small.scaled(
            src.width(), src.height(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def paintEvent(self, _event):
        p = QPainter(self)
        if self._blurred and not self._blurred.isNull():
            p.drawPixmap(0, 0, self._blurred)
        p.fillRect(self.rect(), QColor(0, 0, 0, 120))
        p.end()


class _DeleteProgressDialog(QDialog):
    """Animated modal that shows delete progress with a bar and counter."""

    def __init__(self, total, theme="dark", parent=None):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(400, 200)
        self._total = total
        self._done = 0
        self._deleted_files = 0
        c = palette(theme)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setStyleSheet(
            f"QFrame {{ background-color: {c['bg']};"
            f" border: 1px solid {c['border']}; border-radius: 16px; }}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Deleting Records...")
        title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {c['text']};"
                            " border: none; background: transparent;")
        layout.addWidget(title)

        self._status = QLabel(f"0 / {total}")
        self._status.setStyleSheet(f"font-size: 13px; color: {c['muted']};"
                                   " border: none; background: transparent;")
        layout.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setRange(0, total)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(10)
        self._bar.setStyleSheet(
            f"QProgressBar {{ border: none; border-radius: 5px;"
            f" background-color: {c['panel_alt']}; }}"
            f"QProgressBar::chunk {{ background-color: {c['danger']};"
            f" border-radius: 5px; }}")
        layout.addWidget(self._bar)

        self._detail = QLabel("")
        self._detail.setStyleSheet(f"font-size: 11px; color: {c['muted']};"
                                   " border: none; background: transparent;")
        layout.addWidget(self._detail)
        layout.addStretch()
        outer.addWidget(card)

    def update_progress(self, done, total):
        self._done = done
        self._bar.setValue(done)
        self._status.setText(f"{done} / {total}")
        pct = int(done / max(1, total) * 100)
        self._detail.setText(f"{pct}% complete")
        QApplication.processEvents()

    def set_finished(self, deleted_rows, deleted_files):
        self._status.setText(f"Deleted {deleted_rows} records, {deleted_files} files removed")
        self._bar.setValue(self._total)
        self._detail.setText("Done!")

    def showEvent(self, event):
        super().showEvent(event)
        if self.parent():
            pg = self.parent().window().geometry()
            self.move(pg.x() + (pg.width() - self.width()) // 2,
                      pg.y() + (pg.height() - self.height()) // 2)


class _ClippedFrame(QFrame):
    """QFrame that clips all child painting to its rounded rect."""

    def __init__(self, radius=14, parent=None):
        super().__init__(parent)
        self._radius = radius

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), self._radius, self._radius)
        p.setClipPath(path)
        p.end()
        super().paintEvent(event)


class RecordDetailDialog(QDialog):
    """Modal with blurred backdrop, showing a single record with prev/next."""

    def __init__(self, rows, index, theme="dark", parent=None):
        super().__init__(parent)
        self.setObjectName("recordDetail")
        self.setWindowTitle("Attendance Detail")
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumSize(540, 560)
        self.resize(560, 600)
        self._rows = rows
        self._index = index
        self._theme = theme
        self._overlay = None
        self._build()
        self._show_record()

    def _build(self):
        c = palette(self._theme)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # main card container
        self._card = QFrame()
        self._card.setObjectName("card")
        self._card.setStyleSheet(
            f"QFrame#card {{ background-color: {c['bg']};"
            f" border: 1px solid {c['border']}; border-radius: 16px; }}")
        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)

        # nav header
        nav = QHBoxLayout()
        nav.setSpacing(8)
        self._title = QLabel("")
        self._title.setObjectName("detailTitle")
        nav.addWidget(self._title, 1)
        self._counter = QLabel("")
        self._counter.setObjectName("muted")
        nav.addWidget(self._counter)
        self._prev_btn = QPushButton("  Prev")
        self._prev_btn.setObjectName("pageButton")
        self._prev_btn.setFixedSize(80, 32)
        apply_button_icon(self._prev_btn, "chevron-left", c["text_secondary"])
        self._prev_btn.clicked.connect(lambda: self._navigate(-1))
        nav.addWidget(self._prev_btn)
        self._next_btn = QPushButton("Next  ")
        self._next_btn.setObjectName("pageButton")
        self._next_btn.setFixedSize(80, 32)
        apply_button_icon(self._next_btn, "chevron-right", c["text_secondary"])
        self._next_btn.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._next_btn.clicked.connect(lambda: self._navigate(1))
        nav.addWidget(self._next_btn)
        close_x = QPushButton()
        close_x.setObjectName("iconButtonFlat")
        close_x.setFixedSize(32, 32)
        apply_button_icon(close_x, "x", c["muted"])
        close_x.clicked.connect(self.accept)
        nav.addWidget(close_x)
        layout.addLayout(nav)

        # image frame — clip children to the rounded rect
        self._img_frame = _ClippedFrame(radius=14)
        self._img_frame.setObjectName("detailImageFrame")
        self._img_frame.setFixedHeight(DETAIL_IMG_SIZE)
        img_layout = QVBoxLayout(self._img_frame)
        img_layout.setContentsMargins(0, 0, 0, 0)
        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setScaledContents(False)
        self._img_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._img_label.mousePressEvent = self._open_full_image
        img_layout.addWidget(self._img_label)
        layout.addWidget(self._img_frame)

        # detail grid
        detail_card = QFrame()
        detail_card.setObjectName("subtlePanel")
        grid = QGridLayout(detail_card)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._fields = {}
        for label_text, row, col in [
            ("Employee", 0, 0), ("Employee ID", 0, 2),
            ("Recorded at", 1, 0), ("Status", 1, 2),
            ("Duration", 2, 0), ("Event ID", 2, 2),
        ]:
            lbl = QLabel(label_text)
            lbl.setObjectName("detailLabel")
            val = QLabel("—")
            val.setObjectName("detailValue")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(lbl, row, col)
            grid.addWidget(val, row, col + 1)
            self._fields[label_text] = val
        layout.addWidget(detail_card)
        layout.addStretch()

        # footer
        footer = QHBoxLayout()
        self._open_btn = QPushButton("Open full image")
        apply_button_icon(self._open_btn, "expand", c["text_secondary"])
        self._open_btn.clicked.connect(lambda: self._open_full_image(None))
        footer.addWidget(self._open_btn)
        footer.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)
        outer.addWidget(self._card)

    def showEvent(self, event):
        super().showEvent(event)
        if self.parent() and not self._overlay:
            self._overlay = _BlurOverlay(self.parent())
        self._center()

    def _center(self):
        if self.parent():
            parent_geo = self.parent().window().geometry()
            x = parent_geo.x() + (parent_geo.width() - self.width()) // 2
            y = parent_geo.y() + (parent_geo.height() - self.height()) // 2
            self.move(x, y)

    def closeEvent(self, event):
        if self._overlay:
            self._overlay.hide()
            self._overlay.deleteLater()
            self._overlay = None
        super().closeEvent(event)

    def reject(self):
        if self._overlay:
            self._overlay.hide()
            self._overlay.deleteLater()
            self._overlay = None
        super().reject()

    def accept(self):
        if self._overlay:
            self._overlay.hide()
            self._overlay.deleteLater()
            self._overlay = None
        super().accept()

    def _navigate(self, delta):
        new = self._index + delta
        if 0 <= new < len(self._rows):
            self._index = new
            self._show_record()

    def _show_record(self):
        row = self._rows[self._index]
        self._title.setText(row["name"] or "Unknown")
        self._counter.setText(f"{self._index + 1} of {len(self._rows)}")
        self._prev_btn.setEnabled(self._index > 0)
        self._next_btn.setEnabled(self._index < len(self._rows) - 1)

        snap = row.get("snapshot", "")
        if snap and Path(snap).is_file():
            pixmap = QPixmap(snap)
            if not pixmap.isNull():
                fw = self._img_frame.width()
                fh = DETAIL_IMG_SIZE
                scaled = pixmap.scaled(
                    fw, fh,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                # paint the scaled image into a rounded-rect pixmap
                result = QPixmap(scaled.size())
                result.fill(Qt.GlobalColor.transparent)
                p = QPainter(result)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                path = QPainterPath()
                path.addRoundedRect(QRectF(0, 0, scaled.width(), scaled.height()), 12, 12)
                p.setClipPath(path)
                p.drawPixmap(0, 0, scaled)
                p.end()
                self._img_label.setPixmap(result)
            else:
                self._img_label.setText("Image could not be loaded")
            self._open_btn.setEnabled(True)
        else:
            self._img_label.setPixmap(_placeholder_thumb(120, self._theme))
            self._open_btn.setEnabled(False)

        self._fields["Employee"].setText(row["name"] or "—")
        self._fields["Employee ID"].setText(row["employee_id"] or "—")
        self._fields["Recorded at"].setText(row["time"] or "—")
        self._fields["Status"].setText(row["status"] or "—")
        self._fields["Duration"].setText(f"{row['duration']:.1f}s")
        eid = row.get("event_id", "") or ""
        self._fields["Event ID"].setText((eid[:20] + "...") if len(eid) > 20 else eid or "—")
        self._fields["Event ID"].setToolTip(eid)

    def _open_full_image(self, _event):
        row = self._rows[self._index]
        snap = row.get("snapshot", "")
        if snap and Path(snap).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(snap))

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Left:
            self._navigate(-1)
        elif event.key() == Qt.Key.Key_Right:
            self._navigate(1)
        elif event.key() == Qt.Key.Key_Escape:
            self.accept()
        else:
            super().keyPressEvent(event)


# ══════════════════════════════════════════════════════════════════════════════
#  Main screen
# ══════════════════════════════════════════════════════════════════════════════

class ReportScreen(QWidget):
    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self.reader = AttendanceReader(self.settings.db_path)
        self.page = 0
        self.total = 0
        self.cards = {}
        self._thumb_cache = {}
        self._current_rows = []
        self._build()
        self._connect()
        self.reload()

    # ══════════════════════════════════════════════════════════════════════════
    #  Layout
    # ══════════════════════════════════════════════════════════════════════════

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(12)

        # stat cards
        grid = QGridLayout()
        grid.setSpacing(10)
        for idx, (key, title, value, hint, icon, tone) in enumerate((
                ("records", "Total Records", "0", "Check-ins in range", "list", "blue"),
                ("employees", "Employees", "0", "Distinct employees", "users", "green"),
                ("days", "Days Present", "0", "Distinct local days", "calendar", "purple"),
                ("presence", "Total Presence", "0m", "Verified time", "clock", "orange"),
        )):
            card = StatCard(title, value, hint=hint, icon=icon, tone=tone)
            self.cards[key] = card
            grid.addWidget(card, 0, idx)
        layout.addLayout(grid)

        layout.addWidget(self._build_filters())

        self._selection_bar = self._build_selection_bar()
        self._selection_bar.setVisible(False)
        layout.addWidget(self._selection_bar)

        layout.addWidget(self._build_table(), 1)
        layout.addWidget(self._build_pagination())

        self.toast = ToastBar(theme=self._theme)
        layout.addWidget(self.toast)

    # ── filters ───────────────────────────────────────────────────────────

    def _build_filters(self):
        c = palette(self._theme)
        bar = QFrame()
        bar.setObjectName("card")
        self.filter_card = bar
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(10)

        # ── row 1: preset chips + date range + search ──
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        # quick preset chips
        self._preset_btns = {}
        for label, key in (("Today", "today"), ("7 Days", "week"),
                           ("30 Days", "month"), ("All", "all")):
            btn = QPushButton(label)
            btn.setObjectName("filterChip")
            btn.setFixedHeight(30)
            btn.setCheckable(True)
            btn.clicked.connect(lambda checked=False, k=key: self._apply_preset(k))
            row1.addWidget(btn)
            self._preset_btns[key] = btn
        self._preset_btns["week"].setChecked(True)

        # separator
        sep = QFrame()
        sep.setObjectName("hLine")
        sep.setFixedSize(1, 22)
        row1.addWidget(sep)

        # date range group
        today = date.today()
        self.start_edit = QDateEdit(today - timedelta(days=6))
        self.end_edit = QDateEdit(today)
        for w in (self.start_edit, self.end_edit):
            w.setCalendarPopup(True)
            w.setDisplayFormat("yyyy-MM-dd")
            w.setFixedWidth(130)
            w.setFixedHeight(30)
        row1.addWidget(self.start_edit)
        dash = QLabel("–")
        dash.setObjectName("muted")
        dash.setFixedWidth(12)
        dash.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row1.addWidget(dash)
        row1.addWidget(self.end_edit)

        row1.addStretch(1)

        # search
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search employee...")
        self.search_edit.setFixedHeight(30)
        self.search_edit.setMinimumWidth(160)
        self.search_edit.setMaximumWidth(240)
        row1.addWidget(self.search_edit)

        outer.addLayout(row1)

        # ── row 2: status + rows + actions ──
        row2 = QHBoxLayout()
        row2.setSpacing(8)

        self.status_filter = QComboBox()
        self.status_filter.addItems(["All status", "KNOWN", "UNKNOWN"])
        self.status_filter.setFixedHeight(30)
        self.status_filter.setMinimumWidth(110)
        row2.addWidget(self.status_filter)

        rows_lbl = QLabel("Rows")
        rows_lbl.setObjectName("muted")
        row2.addWidget(rows_lbl)
        self.page_size_box = QSpinBox()
        self.page_size_box.setRange(10, 500)
        self.page_size_box.setValue(max(10, int(self.settings.report_page_size)))
        self.page_size_box.setFixedWidth(68)
        self.page_size_box.setFixedHeight(30)
        row2.addWidget(self.page_size_box)

        row2.addStretch(1)

        self.refresh_button = QPushButton()
        self.refresh_button.setObjectName("iconButton")
        self.refresh_button.setFixedSize(32, 32)
        self.refresh_button.setToolTip("Refresh")
        apply_button_icon(self.refresh_button, "refresh", c["muted"])
        row2.addWidget(self.refresh_button)

        self.clear_button = QPushButton("Reset")
        self.clear_button.setObjectName("ghostButton")
        self.clear_button.setFixedHeight(30)
        row2.addWidget(self.clear_button)

        self.apply_button = QPushButton("Apply Filters")
        self.apply_button.setObjectName("primary")
        self.apply_button.setFixedHeight(30)
        apply_button_icon(self.apply_button, "check", c["primary_fg"])
        row2.addWidget(self.apply_button)

        self.export_button = QPushButton("Export")
        self.export_button.setObjectName("softButton")
        self.export_button.setFixedHeight(30)
        apply_button_icon(self.export_button, "download", c["primary"])
        self.export_button.setProperty("overflowMenu", True)
        self._build_export_menu()
        row2.addWidget(self.export_button)

        outer.addLayout(row2)
        return bar

    def _apply_preset(self, key):
        for k, btn in self._preset_btns.items():
            btn.setChecked(k == key)
        today = date.today()
        if key == "today":
            self.start_edit.setDate(today)
            self.end_edit.setDate(today)
        elif key == "week":
            self.start_edit.setDate(today - timedelta(days=6))
            self.end_edit.setDate(today)
        elif key == "month":
            self.start_edit.setDate(today - timedelta(days=29))
            self.end_edit.setDate(today)
        elif key == "all":
            self.start_edit.setDate(today - timedelta(days=365 * 5))
            self.end_edit.setDate(today)
        self.reload(reset_page=True)

    def _build_export_menu(self):
        c = palette(self._theme)
        menu = QMenu(self.export_button)
        section = menu.addAction("Export scope")
        section.setEnabled(False)
        menu.addSeparator()
        for attr, label, icon_name, scope in (
            ("_export_current", "Current page", "list", "current"),
            ("_export_all", "All pages", "database", "all"),
            ("_export_to_first", "Current page to first", "chevron-up", "to_first"),
            ("_export_to_last", "Current page to last", "chevron-down", "to_last"),
        ):
            act = menu.addAction(label)
            act.setIcon(make_icon(icon_name, 16, c["text_secondary"]))
            act.triggered.connect(lambda checked=False, s=scope: self._export(s))
            setattr(self, attr, act)
        menu.addSeparator()
        self._export_selected = menu.addAction("Selected rows only")
        self._export_selected.setIcon(make_icon("check", 16, c["text_secondary"]))
        self._export_selected.triggered.connect(lambda: self._export("selected"))
        menu.addSeparator()
        fmt_section = menu.addAction("Format")
        fmt_section.setEnabled(False)
        menu.addSeparator()
        self._fmt_excel = menu.addAction("Excel (.xlsx)")
        self._fmt_excel.setCheckable(True)
        self._fmt_excel.setChecked(True)
        self._fmt_csv = menu.addAction("CSV (.csv)")
        self._fmt_csv.setCheckable(True)
        self._fmt_excel.triggered.connect(lambda: self._set_format("xlsx"))
        self._fmt_csv.triggered.connect(lambda: self._set_format("csv"))
        self._export_format = "xlsx"
        self.export_button.setMenu(menu)

    def _set_format(self, fmt):
        self._export_format = fmt
        self._fmt_excel.setChecked(fmt == "xlsx")
        self._fmt_csv.setChecked(fmt == "csv")

    # ── selection bar ─────────────────────────────────────────────────────

    def _build_selection_bar(self):
        c = palette(self._theme)
        bar = QFrame()
        bar.setObjectName("selectionBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(10)
        self._sel_count_label = QLabel("0 selected")
        self._sel_count_label.setObjectName("selectionCount")
        row.addWidget(self._sel_count_label)
        row.addStretch(1)
        self._sel_export_btn = QPushButton("Export selected")
        self._sel_export_btn.setObjectName("softButton")
        self._sel_export_btn.setFixedHeight(30)
        apply_button_icon(self._sel_export_btn, "download", c["primary"])
        self._sel_export_btn.clicked.connect(lambda: self._export("selected"))
        row.addWidget(self._sel_export_btn)
        self._sel_delete_btn = QPushButton("Delete selected")
        self._sel_delete_btn.setObjectName("controlButtonDanger")
        self._sel_delete_btn.setFixedHeight(30)
        apply_button_icon(self._sel_delete_btn, "trash", c["danger"])
        self._sel_delete_btn.clicked.connect(self._delete_selected)
        row.addWidget(self._sel_delete_btn)
        self._sel_clear_btn = QPushButton("Clear selection")
        self._sel_clear_btn.setFixedHeight(30)
        self._sel_clear_btn.clicked.connect(self._clear_selection)
        row.addWidget(self._sel_clear_btn)
        return bar

    # ── table ─────────────────────────────────────────────────────────────

    def _build_table(self):
        card = Card("Records", "", icon="list", theme=self._theme)
        self.table_card = card

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.setIconSize(QSize(THUMB_SIZE, THUMB_SIZE))

        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        # col: 0=check 1=image 2=recorded 3=employee 4=id 5=status 6=duration
        for col, width in ((0, 36), (1, 56), (2, 145), (4, 110), (5, 120), (6, 75)):
            self.table.setColumnWidth(col, width)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setMinimumSectionSize(36)

        # header checkbox overlaid on col 0
        self._header_check = QCheckBox(self.table)
        self._header_check.setToolTip("Select all on this page")
        self._header_check.stateChanged.connect(self._toggle_all)
        self._position_header_check()
        hdr.sectionResized.connect(lambda *_: self._position_header_check())

        self.table.setMinimumHeight(250)
        card.add(self.table, 1)

        self.result_label = QLabel("")
        self.result_label.setObjectName("screenSubtitle")
        card.add(self.result_label)
        return card

    def _position_header_check(self):
        hdr = self.table.horizontalHeader()
        x = hdr.sectionPosition(0) + (hdr.sectionSize(0) - self._header_check.sizeHint().width()) // 2
        y = (hdr.height() - self._header_check.sizeHint().height()) // 2
        self._header_check.move(x, y)
        self._header_check.raise_()

    # ── pagination ────────────────────────────────────────────────────────

    def _build_pagination(self):
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self.prev_button = QPushButton("Prev")
        self.prev_button.setObjectName("pageButton")
        self.prev_button.setFixedSize(56, 30)
        row.addWidget(self.prev_button)

        self._page_button_container = QHBoxLayout()
        self._page_button_container.setSpacing(3)
        row.addLayout(self._page_button_container)

        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("pageButton")
        self.next_button.setFixedSize(56, 30)
        row.addWidget(self.next_button)

        row.addSpacing(10)
        self.page_info = QLabel("")
        self.page_info.setObjectName("muted")
        row.addWidget(self.page_info)
        row.addStretch(1)
        self.outbox_label = QLabel("")
        self.outbox_label.setObjectName("muted")
        row.addWidget(self.outbox_label)
        return container

    # ══════════════════════════════════════════════════════════════════════════
    #  Wiring
    # ══════════════════════════════════════════════════════════════════════════

    def _connect(self):
        self.apply_button.clicked.connect(lambda: self.reload(reset_page=True))
        self.refresh_button.clicked.connect(lambda: self.reload(reset_page=True))
        self.search_edit.returnPressed.connect(lambda: self.reload(reset_page=True))
        self.page_size_box.valueChanged.connect(lambda _: self.reload(reset_page=True))
        self.start_edit.dateChanged.connect(lambda _: self.reload(reset_page=True))
        self.end_edit.dateChanged.connect(lambda _: self.reload(reset_page=True))
        self.status_filter.currentIndexChanged.connect(lambda _: self.reload(reset_page=True))
        self.prev_button.clicked.connect(lambda: self._go_page(self.page - 1))
        self.next_button.clicked.connect(lambda: self._go_page(self.page + 1))
        self.clear_button.clicked.connect(self._clear_filters)
        self.table.cellClicked.connect(self._on_cell_click)
        self.engine.attendanceSaved.connect(lambda _: self.reload())

    def _clear_filters(self):
        self.search_edit.clear()
        self.status_filter.setCurrentIndex(0)
        today = date.today()
        self.start_edit.setDate(today - timedelta(days=6))
        self.end_edit.setDate(today)
        self.reload(reset_page=True)

    def selected_range(self):
        start = datetime.combine(self.start_edit.date().toPyDate(), time.min).timestamp()
        end = datetime.combine(self.end_edit.date().toPyDate() + timedelta(days=1), time.min).timestamp()
        if end <= start:
            end = start + 86400
        return start, end

    # ══════════════════════════════════════════════════════════════════════════
    #  Checkbox selection
    # ══════════════════════════════════════════════════════════════════════════

    def _toggle_all(self, state):
        checked = state == Qt.CheckState.Checked.value
        for row_idx in range(self.table.rowCount()):
            cb = self._row_checkbox(row_idx)
            if cb:
                cb.setChecked(checked)
        self._update_selection_bar()

    def _row_checkbox(self, row_idx):
        w = self.table.cellWidget(row_idx, 0)
        if w:
            return w.findChild(QCheckBox)
        return None

    def _selected_indices(self):
        return [i for i in range(self.table.rowCount())
                if (cb := self._row_checkbox(i)) and cb.isChecked()]

    def _update_selection_bar(self):
        count = len(self._selected_indices())
        self._selection_bar.setVisible(count > 0)
        self._sel_count_label.setText(f"{count} selected")
        self._header_check.blockSignals(True)
        if count == 0:
            self._header_check.setChecked(False)
        elif count == self.table.rowCount():
            self._header_check.setChecked(True)
        self._header_check.blockSignals(False)

    def _clear_selection(self):
        self._header_check.blockSignals(True)
        self._header_check.setChecked(False)
        self._header_check.blockSignals(False)
        for row_idx in range(self.table.rowCount()):
            cb = self._row_checkbox(row_idx)
            if cb:
                cb.setChecked(False)
        self._update_selection_bar()

    # ══════════════════════════════════════════════════════════════════════════
    #  Pagination
    # ══════════════════════════════════════════════════════════════════════════

    def _go_page(self, page):
        size = max(1, self.page_size_box.value())
        pages = max(1, (self.total + size - 1) // size)
        self.page = min(max(0, page), pages - 1)
        self.reload()

    def _update_page_buttons(self, current, total_pages):
        while self._page_button_container.count():
            child = self._page_button_container.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        if total_pages <= 1:
            return
        for entry in self._page_numbers(current, total_pages):
            if entry is None:
                dots = QLabel("...")
                dots.setObjectName("muted")
                dots.setFixedSize(24, 30)
                dots.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._page_button_container.addWidget(dots)
            else:
                btn = QPushButton(str(entry + 1))
                btn.setFixedSize(34, 30)
                btn.setObjectName("pageActive" if entry == current else "pageButton")
                btn.clicked.connect(lambda checked=False, p=entry: self._go_page(p))
                self._page_button_container.addWidget(btn)

    @staticmethod
    def _page_numbers(current, total):
        if total <= 7:
            return list(range(total))
        pages = [0]
        if current > 2:
            pages.append(None)
        for p in range(max(1, current - 1), min(total - 1, current + 2)):
            pages.append(p)
        if current < total - 3:
            pages.append(None)
        pages.append(total - 1)
        return pages

    # ══════════════════════════════════════════════════════════════════════════
    #  Data loading
    # ══════════════════════════════════════════════════════════════════════════

    def reload(self, reset_page=False):
        if reset_page:
            self.page = 0
        start, end = self.selected_range()
        search = self.search_edit.text().strip() or None
        status_text = self.status_filter.currentText()

        size = max(1, self.page_size_box.value())
        self.total = self.reader.count(start=start, end=end, search=search)
        pages = max(1, (self.total + size - 1) // size)
        self.page = min(self.page, pages - 1)
        rows = self.reader.records(start=start, end=end, search=search,
                                   limit=size, offset=self.page * size)
        if status_text not in ("All status", "All"):
            rows = [r for r in rows if r["status"].upper() == status_text]

        self._current_rows = rows
        self._populate_table(rows)

        summary = self.reader.summary(start=start, end=end, search=search)
        counts = self.reader.outbox_counts()
        self.cards["records"].set_value(summary["records"])
        self.cards["employees"].set_value(summary["employees"])
        self.cards["days"].set_value(summary["days"])
        self.cards["presence"].set_value(f"{summary['duration'] / 60:.1f}m")

        self.page_info.setText(f"Page {self.page + 1} of {pages}  •  {self.total} records")
        self.outbox_label.setText(f"Outbox: {counts['pending']} pending, {counts['synced']} synced")
        self.prev_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page + 1 < pages)
        self._update_page_buttons(self.page, pages)

        d1 = self.start_edit.date().toString("yyyy-MM-dd")
        d2 = self.end_edit.date().toString("yyyy-MM-dd")
        self.result_label.setText(
            f"Showing {len(rows)} of {self.total} records  •  {d1} to {d2}"
            + (f"  •  \"{search}\"" if search else "")
            + (f"  •  {status_text}" if status_text not in ("All status", "All") else ""))

        self._selection_bar.setVisible(False)
        self._header_check.blockSignals(True)
        self._header_check.setChecked(False)
        self._header_check.blockSignals(False)

        if self.reader.error:
            self.toast.show_message(self.reader.error, "bad")

    def _populate_table(self, rows):
        self.table.setRowCount(len(rows))
        c = palette(self._theme)

        for idx, row in enumerate(rows):
            # col 0: checkbox
            cb_w = QWidget()
            cb_w.setStyleSheet("background: transparent;")
            cbl = QHBoxLayout(cb_w)
            cbl.setContentsMargins(0, 0, 0, 0)
            cbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb = QCheckBox()
            cb.stateChanged.connect(lambda _: self._update_selection_bar())
            cbl.addWidget(cb)
            self.table.setCellWidget(idx, 0, cb_w)

            # col 1: evidence thumbnail
            snap = row["snapshot"]
            tw = QWidget()
            tw.setStyleSheet("background: transparent;")
            tl = QHBoxLayout(tw)
            tl.setContentsMargins(4, 2, 4, 2)
            lbl = QLabel()
            lbl.setFixedSize(THUMB_SIZE, THUMB_SIZE)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if snap and Path(snap).is_file():
                px = self._get_thumb(snap)
                if px and not px.isNull():
                    lbl.setPixmap(px)
                    lbl.setCursor(Qt.CursorShape.PointingHandCursor)
                    lbl.setToolTip(Path(snap).name)
                else:
                    lbl.setPixmap(_placeholder_thumb(THUMB_SIZE, self._theme))
            else:
                lbl.setPixmap(_placeholder_thumb(THUMB_SIZE, self._theme))
            tl.addWidget(lbl)
            self.table.setCellWidget(idx, 1, tw)

            # col 2: recorded time
            time_item = QTableWidgetItem(row["time"])
            time_item.setToolTip(row["timestamp"])
            self.table.setItem(idx, 2, time_item)

            # col 3: employee name
            name_item = QTableWidgetItem(row["name"])
            name_item.setToolTip(row["name"])
            font = name_item.font()
            font.setWeight(QFont.Weight.DemiBold)
            name_item.setFont(font)
            self.table.setItem(idx, 3, name_item)

            # col 4: employee id
            self.table.setItem(idx, 4, QTableWidgetItem(row["employee_id"]))

            # col 5: status as text with color
            status_text = row["status"]
            status_item = QTableWidgetItem(status_text)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if status_text == "KNOWN":
                status_item.setForeground(QColor(c["success"]))
            else:
                status_item.setForeground(QColor(c["warn"]))
            font_s = status_item.font()
            font_s.setWeight(QFont.Weight.Bold)
            font_s.setPointSize(max(1, font_s.pointSize() - 1))
            status_item.setFont(font_s)
            self.table.setItem(idx, 5, status_item)

            # col 6: duration
            dur = QTableWidgetItem(f"{row['duration']:.1f}s")
            dur.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(idx, 6, dur)

            self.table.setRowHeight(idx, max(52, THUMB_SIZE + 12))

    def _get_thumb(self, path):
        cached = self._thumb_cache.get(path)
        if cached is not None:
            return cached
        try:
            px = QPixmap(str(path))
        except Exception:
            return None
        if px.isNull():
            return None
        rounded = _rounded_pixmap(px, THUMB_SIZE)
        self._thumb_cache[path] = rounded
        return rounded

    # ══════════════════════════════════════════════════════════════════════════
    #  Row click → detail modal
    # ══════════════════════════════════════════════════════════════════════════

    def _on_cell_click(self, row, col):
        if col == 0:
            return
        if 0 <= row < len(self._current_rows):
            dlg = RecordDetailDialog(self._current_rows, row, self._theme, self)
            dlg.exec()

    # ══════════════════════════════════════════════════════════════════════════
    #  Export
    # ══════════════════════════════════════════════════════════════════════════

    def _export(self, scope):
        start, end = self.selected_range()
        search = self.search_edit.text().strip() or None
        size = max(1, self.page_size_box.value())
        pages = max(1, (self.total + size - 1) // size)

        if scope == "selected":
            indices = self._selected_indices()
            if not indices:
                self.toast.show_message("No rows selected. Check some rows first.", "warn")
                return
            rows = [self._current_rows[i] for i in indices if i < len(self._current_rows)]
            scope_label = f"{len(rows)} selected"
        elif scope == "current":
            rows = self.reader.records(start=start, end=end, search=search,
                                       limit=size, offset=self.page * size)
            scope_label = f"page {self.page + 1}"
        elif scope == "all":
            rows = self.reader.records(start=start, end=end, search=search, limit=100000)
            scope_label = "all pages"
        elif scope == "to_first":
            rows = self.reader.records(start=start, end=end, search=search,
                                       limit=(self.page + 1) * size, offset=0)
            scope_label = f"pages 1–{self.page + 1}"
        elif scope == "to_last":
            rows = self.reader.records(start=start, end=end, search=search,
                                       limit=100000, offset=self.page * size)
            scope_label = f"pages {self.page + 1}–{pages}"
        else:
            return

        if not rows:
            self.toast.show_message("Nothing to export for the current filters.", "warn")
            return

        ext = "xlsx" if self._export_format == "xlsx" else "csv"
        default = str(Path(self.settings.capture_dir).parent /
                      f"attendance_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{ext}")
        filt = "Excel workbook (*.xlsx)" if ext == "xlsx" else "CSV file (*.csv)"
        target, _ = QFileDialog.getSaveFileName(self, "Export report", default, filt)
        if not target:
            return

        work_start = self.settings.report_work_start
        try:
            if ext == "xlsx":
                count = export_excel(target, rows, work_start,
                                     punctuality_column=bool(work_start))
            else:
                count = export_rows(target, rows, work_start,
                                    punctuality_column=bool(work_start))
        except (OSError, ImportError) as exc:
            self.toast.show_message(f"Export failed: {exc}", "bad")
            return

        note = f" with Punctuality (work start {work_start})" if work_start else ""
        self.toast.show_message(
            f"Exported {count} rows ({scope_label}) to {Path(target).name}{note}", "ok")

    # ══════════════════════════════════════════════════════════════════════════
    #  Delete selected
    # ══════════════════════════════════════════════════════════════════════════

    def _delete_selected(self):
        indices = self._selected_indices()
        if not indices:
            self.toast.show_message("No rows selected.", "warn")
            return
        rows = [self._current_rows[i] for i in indices if i < len(self._current_rows)]
        if not rows:
            return

        confirm = QMessageBox(self)
        confirm.setWindowTitle("Delete Attendance Records")
        confirm.setText(f"Delete {len(rows)} attendance record(s)?\n\n"
                        "This will permanently remove the database rows and their "
                        "captured snapshot files. This action cannot be undone.")
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Yes:
            return

        record_ids = [r["id"] for r in rows]
        dlg = _DeleteProgressDialog(len(record_ids), self._theme, self)
        overlay = _BlurOverlay(self) if self.isVisible() else None
        dlg.show()
        QApplication.processEvents()

        try:
            deleted_rows, deleted_files = delete_records(
                self.settings.db_path, record_ids,
                delete_snapshots=True,
                progress_cb=dlg.update_progress)
            dlg.set_finished(deleted_rows, deleted_files)
            QApplication.processEvents()
            QTimer.singleShot(1200, dlg.accept)
            dlg.exec()
            # evict deleted snapshots from thumb cache
            for r in rows:
                self._thumb_cache.pop(r.get("snapshot", ""), None)
            self.reader.close()
            self.reader = AttendanceReader(self.settings.db_path)
            self.reload()
            self.toast.show_message(
                f"Deleted {deleted_rows} records and {deleted_files} snapshot files.", "ok")
        except Exception as exc:
            dlg.accept()
            self.toast.show_message(f"Delete failed: {exc}", "bad")
        finally:
            if overlay:
                overlay.hide()
                overlay.deleteLater()

    # ══════════════════════════════════════════════════════════════════════════
    #  Lifecycle
    # ══════════════════════════════════════════════════════════════════════════

    def on_settings_changed(self, settings):
        self.settings = settings
        self._theme = settings.theme if hasattr(settings, "theme") else "dark"
        self.reader.close()
        self.reader = AttendanceReader(self.settings.db_path)
        self._thumb_cache.clear()
        self.reload()

    def closeEvent(self, event):
        self.reader.close()
        super().closeEvent(event)

    def set_theme(self, theme):
        self._theme = theme
        for card in self.cards.values():
            card.set_theme(theme)
        self.table_card.set_theme(theme)
        self.toast.set_theme(theme)
        c = palette(theme)
        apply_button_icon(self.refresh_button, "refresh", c["muted"])
        apply_button_icon(self.export_button, "download", c["primary"])
        self._thumb_cache.clear()
        self.reload()
