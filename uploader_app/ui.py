"""PyQt6 dashboard for the folder upload engine."""
import os
import sys
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .engine import UploadEngine
from .storage import CONFIG_FILE, read_json, write_json

TITLE = "Folder Auto Uploader"
TERMINAL = {"Uploaded", "Failed", "Unchanged", "Cancelled", "File unavailable"}
COLORS = {"Uploaded": "#22c55e", "Failed": "#ef4444", "File unavailable": "#ef4444",
          "Unchanged": "#64748b", "Cancelled": "#f59e0b"}

class EventBridge(QObject):
    event = pyqtSignal(str, object)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle(TITLE); self.resize(1080, 700); self.setMinimumSize(820, 560)
        self.rows = {}; self.bridge = EventBridge(); self.bridge.event.connect(self._event)
        self.engine = UploadEngine(self.bridge.event.emit)
        self._build(); self._load()

    def _build(self):
        root = QWidget(objectName="root"); layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24); layout.setSpacing(16)
        layout.addWidget(QLabel("Auto Upload Monitor", objectName="title"))
        layout.addWidget(QLabel("Watch a folder and send new or changed files in concurrent batches.", objectName="subtitle"))
        card = QFrame(objectName="card"); form = QGridLayout(card); form.setSpacing(12)
        form.addWidget(QLabel("TARGET FOLDER"), 0, 0)
        self.folder = QLineEdit(); self.folder.setPlaceholderText("Choose a folder to monitor")
        browse = QPushButton("Browse"); browse.clicked.connect(self._browse)
        form.addWidget(self.folder, 1, 0); form.addWidget(browse, 1, 1)
        form.addWidget(QLabel("UPLOAD URL"), 2, 0)
        self.url = QLineEdit(); self.url.setPlaceholderText("https://example.com/api/upload")
        form.addWidget(self.url, 3, 0, 1, 2)
        options = QHBoxLayout(); self.recursive = QCheckBox("Detect files in sub-folders"); self.recursive.setChecked(True)
        options.addWidget(self.recursive); options.addStretch(); options.addWidget(QLabel("Concurrent uploads"))
        self.batch = QSpinBox(); self.batch.setRange(1, 50); self.batch.setValue(3); options.addWidget(self.batch)
        form.addLayout(options, 4, 0, 1, 2); layout.addWidget(card)
        controls = QHBoxLayout()
        self.start_btn = QPushButton("Start monitoring", objectName="primary")
        self.stop_btn = QPushButton("Stop"); self.stop_btn.setEnabled(False)
        self.scan_btn = QPushButton("Upload existing files"); self.scan_btn.setEnabled(False)
        clear_btn = QPushButton("Clear activity")
        self.start_btn.clicked.connect(self._start); self.stop_btn.clicked.connect(self._stop)
        self.scan_btn.clicked.connect(self._scan); clear_btn.clicked.connect(self._clear)
        for button in (self.start_btn, self.stop_btn, self.scan_btn): controls.addWidget(button)
        controls.addStretch(); controls.addWidget(clear_btn); layout.addLayout(controls)
        status_row = QHBoxLayout(); self.dot = QLabel("●", objectName="dot"); self.status = QLabel("Ready")
        self.summary = QLabel("0 active  •  0 uploaded  •  0 failed", objectName="summary")
        status_row.addWidget(self.dot); status_row.addWidget(self.status); status_row.addStretch(); status_row.addWidget(self.summary)
        layout.addLayout(status_row)
        self.table = QTableWidget(0, 3); self.table.setHorizontalHeaderLabels(["FILE", "STATUS", "DETAILS"])
        self.table.verticalHeader().hide(); self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader(); header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents); header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        self.progress = QProgressBar(); self.progress.setTextVisible(False); self.progress.setRange(0, 1)
        layout.addWidget(self.progress); self.setCentralWidget(root); self.setStyleSheet(STYLE)

    def _load(self):
        config = read_json(CONFIG_FILE)
        self.folder.setText(config.get("folder", str(Path.cwd())))
        self.url.setText(config.get("upload_url", os.getenv("UPLOAD_URL", "")))
        self.recursive.setChecked(config.get("recursive", True)); self.batch.setValue(config.get("batch_size", 3))

    def _browse(self):
        selected = QFileDialog.getExistingDirectory(self, "Select target folder", self.folder.text())
        if selected: self.folder.setText(selected)

    def _start(self):
        folder, url = self.folder.text().strip(), self.url.text().strip()
        if not folder: QMessageBox.warning(self, TITLE, "Select a target folder."); return
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, TITLE, "Enter a valid HTTP upload URL."); return
        try:
            self.engine.start(folder, url, self.recursive.isChecked(), self.batch.value())
            write_json(CONFIG_FILE, {"folder": folder, "upload_url": url,
                "recursive": self.recursive.isChecked(), "batch_size": self.batch.value()})
        except OSError as exc: QMessageBox.critical(self, TITLE, f"Could not monitor folder:\n{exc}")

    def _stop(self):
        self.status.setText("Stopping…")
        threading.Thread(target=self.engine.stop, kwargs={"wait": True}, daemon=True).start()

    def _scan(self): self.status.setText(f"Queued {self.engine.scan_existing()} existing file(s)")
    def _clear(self): self.table.setRowCount(0); self.rows.clear(); self._summary()

    def _event(self, kind, payload):
        if kind == "monitor":
            running = payload["running"]; self.status.setText(payload["message"])
            self.dot.setProperty("running", running); self.dot.style().unpolish(self.dot); self.dot.style().polish(self.dot)
            self.start_btn.setEnabled(not running); self.stop_btn.setEnabled(running); self.scan_btn.setEnabled(running); return
        key = str(payload.path)
        if key not in self.rows:
            self.table.insertRow(0); self.rows = {path: row + 1 for path, row in self.rows.items()}; self.rows[key] = 0
        row = self.rows[key]
        for column, value in enumerate((payload.relative, payload.status, payload.error)):
            item = QTableWidgetItem(value); item.setToolTip(value)
            if column == 1:
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setForeground(QColor(COLORS.get(payload.status, "#94a3b8")))
            self.table.setItem(row, column, item)
        self._summary()

    def _summary(self):
        statuses = [self.table.item(r, 1).text() for r in range(self.table.rowCount()) if self.table.item(r, 1)]
        done = sum(s in TERMINAL for s in statuses); failed = sum(s in {"Failed", "File unavailable"} for s in statuses)
        self.summary.setText(f"{len(statuses)-done} active  •  {statuses.count('Uploaded')} uploaded  •  {failed} failed")
        self.progress.setRange(0, max(len(statuses), 1)); self.progress.setValue(done)

    def closeEvent(self, event): self.engine.stop(wait=False); event.accept()

STYLE = """
#root { background: #0b1120; color: #e2e8f0; }
QLabel { color: #cbd5e1; font-size: 13px; }
#title { color: white; font-size: 27px; font-weight: 700; }
#subtitle, #summary { color: #94a3b8; }
#card { background: #111827; border: 1px solid #263449; border-radius: 12px; padding: 12px; }
QLineEdit, QSpinBox { background: #0f172a; color: #f8fafc; border: 1px solid #334155; border-radius: 7px; padding: 9px; }
QPushButton { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 7px; padding: 9px 15px; }
QPushButton:hover { background: #334155; } QPushButton:disabled { color: #64748b; }
QPushButton#primary { background: #2563eb; border-color: #3b82f6; color: white; font-weight: 600; }
QTableWidget { background: #0f172a; alternate-background-color: #111c31; color: #dbeafe; border: 1px solid #263449; border-radius: 9px; gridline-color: #1e293b; }
QHeaderView::section { background: #172033; color: #94a3b8; border: 0; padding: 9px; font-weight: 600; }
QProgressBar { background: #1e293b; border: 0; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #3b82f6; border-radius: 3px; }
#dot { color: #64748b; } #dot[running="true"] { color: #22c55e; }
"""

def main():
    application = QApplication(sys.argv); application.setStyle("Fusion")
    window = MainWindow(); window.show(); return application.exec()
