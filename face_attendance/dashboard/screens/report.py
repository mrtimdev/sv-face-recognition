"""Attendance Report: filtered, paged records with summaries and CSV export.

The reader connection is read-only, so this screen can never alter attendance.
Factual aggregates only: the database has no shift/late/absent policy, so
"Punctuality" is an optional label computed at export time from a configured
work start time.
"""
from datetime import date, datetime, time, timedelta
from pathlib import Path

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QComboBox, QDateEdit, QFileDialog, QGridLayout, QHBoxLayout,
                             QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox,
                             QTableView, QVBoxLayout, QWidget)

from ...report import AttendanceReader, export_rows, local_text, preset_range
from ..widgets import StatCard, ToastBar


COLUMNS = ("Recorded (local)", "Employee", "Employee ID", "Status",
           "Presence (s)", "Evidence image", "Event ID")


class AttendanceTableModel(QAbstractTableModel):
    """Rows come from ``AttendanceReader.records``; paging is done in SQL."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole or orientation != Qt.Orientation.Horizontal:
            return None
        return COLUMNS[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return (row["time"], row["name"], row["employee_id"], row["status"],
                    f"{row['duration']:.1f}",
                    Path(row["snapshot"]).name if row["snapshot"] else "",
                    row["event_id"])[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row["snapshot"] or row["event_id"]
        return None


class ReportScreen(QWidget):
    def __init__(self, engine, settings, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = settings
        self.reader = AttendanceReader(self.settings.db_path)
        self.model = AttendanceTableModel(self)
        self.page = 0
        self.total = 0
        self.cards = {}
        self._build()
        self._connect()
        self.reload()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)
        title = QLabel("Attendance Report")
        title.setObjectName("screenTitle")
        self.subtitle = QLabel("Read-only view of the committed attendance table "
                               "(attendance.db is the record of truth)")
        self.subtitle.setObjectName("screenSubtitle")
        layout.addWidget(title)
        layout.addWidget(self.subtitle)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Range"))
        self.preset_box = QComboBox()
        for label, value in (("Today", "today"), ("Last 7 days", "week"),
                             ("This month", "month"), ("Custom", "custom"), ("All time", "all")):
            self.preset_box.addItem(label, value)
        filters.addWidget(self.preset_box)
        today = date.today()
        self.start_edit = QDateEdit(today - timedelta(days=6))
        self.end_edit = QDateEdit(today)
        for widget in (self.start_edit, self.end_edit):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
            widget.setEnabled(False)
            filters.addWidget(widget)
        filters.addSpacing(8)
        filters.addWidget(QLabel("Employee or ID"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name or employee ID")
        filters.addWidget(self.search_edit, 2)
        filters.addWidget(QLabel("Page size"))
        self.page_size_box = QSpinBox()
        self.page_size_box.setRange(10, 1000)
        self.page_size_box.setValue(max(10, int(self.settings.report_page_size)))
        filters.addWidget(self.page_size_box)
        self.refresh_button = QPushButton("Apply filters")
        self.refresh_button.setObjectName("primary")
        filters.addWidget(self.refresh_button)
        self.export_button = QPushButton("Export CSV")
        filters.addWidget(self.export_button)
        layout.addLayout(filters)

        grid = QGridLayout()
        grid.setSpacing(10)
        for index, (key, title, value, hint) in enumerate((
                ("records", "Records", "0", "Committed check-ins"),
                ("employees", "Employees", "0", "Distinct employees"),
                ("days", "Days present", "0", "Distinct local days"),
                ("first", "First check-in", "-", "Earliest in range"),
                ("last", "Last check-out", "-", "Latest in range"),
                ("presence", "Verified presence", "0s", "Sum of verified seconds"),
                ("pending", "Outbox pending", "0", "Not yet acknowledged" ))):
            card = StatCard(title, value, hint)
            self.cards[key] = card
            grid.addWidget(card, 0, index)
        layout.addLayout(grid)

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setToolTip("Double-click the Evidence image column to open a snapshot")
        layout.addWidget(self.table, 1)

        pager = QHBoxLayout()
        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.page_label = QLabel("Page 1")
        self.page_label.setObjectName("muted")
        self.outbox_label = QLabel("")
        self.outbox_label.setObjectName("muted")
        pager.addWidget(self.previous_button)
        pager.addWidget(self.next_button)
        pager.addWidget(self.page_label)
        pager.addStretch(1)
        pager.addWidget(self.outbox_label)
        layout.addLayout(pager)

        self.toast = ToastBar()
        layout.addWidget(self.toast)

    def _connect(self):
        self.preset_box.currentIndexChanged.connect(self._preset_changed)
        self.refresh_button.clicked.connect(lambda: self.reload(reset_page=True))
        self.search_edit.returnPressed.connect(lambda: self.reload(reset_page=True))
        self.page_size_box.valueChanged.connect(lambda _: self.reload(reset_page=True))
        self.start_edit.dateChanged.connect(lambda _: self.reload(reset_page=True))
        self.end_edit.dateChanged.connect(lambda _: self.reload(reset_page=True))
        self.previous_button.clicked.connect(lambda: self._page(-1))
        self.next_button.clicked.connect(lambda: self._page(1))
        self.export_button.clicked.connect(self.export)
        self.table.doubleClicked.connect(self._open_snapshot)
        self.engine.attendanceSaved.connect(lambda result: self.reload())

    def _preset_changed(self):
        custom = self.preset_box.currentData() == "custom"
        self.start_edit.setEnabled(custom)
        self.end_edit.setEnabled(custom)
        self.reload(reset_page=True)

    def selected_range(self):
        """(start, end) epochs; ``None`` means unbounded."""
        preset = self.preset_box.currentData()
        if preset == "custom":
            start = datetime.combine(self.start_edit.date().toPyDate(), time.min).timestamp()
            end = datetime.combine(self.end_edit.date().toPyDate() + timedelta(days=1), time.min).timestamp()
            return (start, end) if end > start else (start, start + 86400)
        return preset_range(preset)

    def _page(self, delta):
        size = max(1, self.page_size_box.value())
        pages = max(1, (self.total + size - 1) // size)
        self.page = min(max(0, self.page + delta), pages - 1)
        self.reload()

    def reload(self, reset_page=False):
        if reset_page:
            self.page = 0
        start, end = self.selected_range()
        search = self.search_edit.text().strip() or None
        size = max(1, self.page_size_box.value())
        self.total = self.reader.count(start=start, end=end, search=search)
        pages = max(1, (self.total + size - 1) // size)
        self.page = min(self.page, pages - 1)
        rows = self.reader.records(start=start, end=end, search=search,
                                   limit=size, offset=self.page * size)
        self.model.set_rows(rows)
        summary = self.reader.summary(start=start, end=end, search=search)
        counts = self.reader.outbox_counts()
        self.cards["records"].set_value(summary["records"])
        self.cards["employees"].set_value(summary["employees"])
        self.cards["days"].set_value(summary["days"])
        self.cards["first"].set_value(local_text(summary["first"], "%Y-%m-%d %H:%M") or "-")
        self.cards["last"].set_value(local_text(summary["last"], "%Y-%m-%d %H:%M") or "-")
        self.cards["presence"].set_value(f"{summary['duration'] / 60:.1f}m")
        self.cards["pending"].set_value(counts["pending"], "warn" if counts["pending"] else "ok")
        self.page_label.setText(f"Page {self.page + 1} of {pages} ({self.total} records)")
        self.outbox_label.setText(f"Outbox: {counts['pending']} pending, {counts['synced']} synced")
        self.previous_button.setEnabled(self.page > 0)
        self.next_button.setEnabled(self.page + 1 < pages)
        if self.reader.error:
            self.toast.show_message(self.reader.error, "bad")

    # --- export and evidence ----------------------------------------------
    def export(self):
        start, end = self.selected_range()
        search = self.search_edit.text().strip() or None
        rows = self.reader.records(start=start, end=end, search=search, limit=100000)
        if not rows:
            self.toast.show_message("Nothing to export for the current filters.", "warn")
            return
        default = str(Path(self.settings.capture_dir).parent /
                      f"attendance_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        target, _ = QFileDialog.getSaveFileName(self, "Export report", default, "CSV file (*.csv)")
        if not target:
            return
        work_start = self.settings.report_work_start
        try:
            count = export_rows(target, rows, work_start, punctuality_column=bool(work_start))
        except OSError as exc:
            self.toast.show_message(f"Export failed: {exc}", "bad")
            return
        note = f" with Punctuality (work start {work_start})" if work_start else ""
        self.toast.show_message(f"Exported {count} rows to {target}{note}", "ok")

    def _open_snapshot(self, index):
        if not index.isValid() or index.column() != 5:
            self.toast.show_message("Double-click the Evidence image column to open a snapshot.", "warn")
            return
        row = self.model.rows[index.row()]
        path = Path(row["snapshot"])
        if not row["snapshot"] or not path.exists():
            self.toast.show_message("That evidence image is not available on this machine.", "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def on_settings_changed(self, settings):
        self.settings = settings
        self.reader.close()
        self.reader = AttendanceReader(self.settings.db_path)
        self.reload()

    def closeEvent(self, event):
        self.reader.close()
        super().closeEvent(event)

    def set_theme(self, theme):
        for card in self.cards.values():
            card.set_theme(theme)