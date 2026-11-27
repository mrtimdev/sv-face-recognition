"""Live Monitor camera controls: a chip per saved camera, and the add/edit dialog.

A chip shows the camera's name and a status dot; clicking it shows or hides
that camera in the monitor, right-clicking edits or removes it. RTSP passwords
never appear in a label or tooltip, only masked by ``describe_source``.
"""
import threading

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                             QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QScrollArea,
                             QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from ...settings import MAX_LIVE_CAMERAS, camera_problem, describe_source, source_kind
from ..icons import make_icon
from ..theme import palette

STATUS_TONES = {"CONNECTED": "success", "CONNECTING": "warn", "RECONNECTING": "warn",
                "CAMERA DISCONNECTED": "danger"}
STATUS_TEXT = {"CONNECTED": "Live", "CONNECTING": "Connecting", "RECONNECTING": "Reconnecting",
               "CAMERA DISCONNECTED": "Offline"}


def dot_icon(color, size=10):
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


def status_color(theme, status, shown=True):
    colors = palette(theme)
    if not shown or status not in STATUS_TONES:
        return colors["muted"]
    return colors[STATUS_TONES[status]]


class CameraBar(QWidget):
    """Chips for every saved camera, in a row above the monitor tiles."""

    toggled = pyqtSignal(int, bool)     # camera index, show it
    addRequested = pyqtSignal()
    editRequested = pyqtSignal(int)
    removeRequested = pyqtSignal(int)
    soloRequested = pyqtSignal(int)     # show only this camera

    def __init__(self, theme="dark", parent=None):
        super().__init__(parent)
        self.setObjectName("cameraBar")
        self._theme = theme
        self._entries, self._shown, self._statuses = [], [], {}
        self.chips = []
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)
        self.title = QLabel("CAMERAS")
        self.title.setObjectName("cameraBarTitle")
        row.addWidget(self.title)
        self._chip_host = QWidget()
        self._chip_row = QHBoxLayout(self._chip_host)
        self._chip_row.setContentsMargins(0, 0, 0, 0)
        self._chip_row.setSpacing(6)
        self._chip_row.addStretch(1)
        scroll = QScrollArea()
        scroll.setObjectName("cameraBarScroll")
        scroll.setWidget(self._chip_host)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedHeight(40)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(scroll, 1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("cameraBarCount")
        row.addWidget(self.count_label)
        self.add_button = QPushButton("+  Add camera")
        self.add_button.setObjectName("cameraBarAdd")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.setToolTip("Add an RTSP camera, webcam or video file")
        self.add_button.clicked.connect(self.addRequested.emit)
        row.addWidget(self.add_button)
        self.set_theme(theme)

    # --- inputs -----------------------------------------------------------
    def set_cameras(self, entries, shown):
        """*entries* from ``Settings.camera_entries``; *shown* the indices on screen."""
        self._entries, self._shown = [dict(entry) for entry in entries], list(shown)
        for chip in self.chips:
            self._chip_row.removeWidget(chip)
            chip.deleteLater()
        self.chips = []
        for index, entry in enumerate(self._entries):
            chip = QToolButton()
            chip.setObjectName("cameraChip")
            chip.setCheckable(True)
            chip.setText(entry["name"])
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            chip.setIconSize(QSize(10, 10))
            chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            chip.clicked.connect(lambda checked, index=index: self._chip_clicked(index, checked))
            chip.customContextMenuRequested.connect(lambda _pos, index=index: self._chip_menu(index))
            self._chip_row.insertWidget(self._chip_row.count() - 1, chip)
            self.chips.append(chip)
        self._refresh()

    def set_statuses(self, statuses):
        """{camera index: engine status}; empty when the engine is stopped."""
        if statuses != self._statuses:
            self._statuses = dict(statuses)
            self._refresh()

    def set_theme(self, theme):
        self._theme = theme
        c = palette(theme)
        self.setStyleSheet(f"""
            QWidget#cameraBar, QScrollArea#cameraBarScroll, QScrollArea#cameraBarScroll > QWidget > QWidget
                {{ background: transparent; }}
            QLabel#cameraBarTitle {{ color: {c['muted']}; font-size: 11px; font-weight: 700;
                                     letter-spacing: 1px; padding-right: 4px; }}
            QScrollArea#cameraBarScroll QScrollBar:horizontal {{ height: 4px; background: transparent; }}
            QScrollArea#cameraBarScroll QScrollBar::handle:horizontal {{ background: {c['border']};
                                                                       border-radius: 2px; }}
            QScrollArea#cameraBarScroll QScrollBar::add-line, QScrollArea#cameraBarScroll QScrollBar::sub-line
                {{ width: 0; }}
            QLabel#cameraBarCount {{ color: {c['muted']}; font-size: 12px; }}
            QToolButton#cameraChip {{
                color: {c['text_secondary']}; background: transparent;
                border: 1px solid {c['border']}; border-radius: 15px;
                padding: 5px 12px 5px 10px; font-size: 13px; font-weight: 600; }}
            QToolButton#cameraChip:hover {{ background: {c['hover']}; }}
            QToolButton#cameraChip:checked {{
                color: {c['text']}; background: {c['primary_soft']};
                border: 1px solid {c['primary']}; }}
            QPushButton#cameraBarAdd {{
                color: {c['primary_soft_fg']}; background: transparent;
                border: 1px dashed {c['border']}; border-radius: 15px;
                padding: 5px 14px; font-size: 13px; font-weight: 600; }}
            QPushButton#cameraBarAdd:hover {{ border-color: {c['primary']}; background: {c['hover']}; }}
        """)
        self._refresh()

    # --- behaviour --------------------------------------------------------
    def _refresh(self):
        for index, chip in enumerate(self.chips):
            shown = index in self._shown
            status = self._statuses.get(index)
            chip.setChecked(shown)
            chip.setIcon(dot_icon(status_color(self._theme, status, shown)))
            entry = self._entries[index]
            state = STATUS_TEXT.get(status, "Shown" if shown else "Hidden")
            chip.setToolTip(f"{entry['name']} · {state}\n{describe_source(entry['source'])}\n"
                            + ("Click to hide · right-click for more" if shown
                               else "Click to show · right-click for more"))
            chip.setAccessibleName(f"{entry['name']}, {state}")
        self.count_label.setText(f"{len(self._shown)}/{MAX_LIVE_CAMERAS} on screen"
                                 if len(self._entries) > 1 else "")

    def _chip_clicked(self, index, checked):
        self._refresh()  # The screen decides; the chip follows the saved choice.
        self.toggled.emit(index, checked)

    def _chip_menu(self, index):
        menu = QMenu(self)
        shown = index in self._shown
        menu.addAction("Hide from monitor" if shown else "Show on monitor",
                       lambda: self.toggled.emit(index, not shown))
        menu.addAction("Show only this camera", lambda: self.soloRequested.emit(index))
        menu.addSeparator()
        menu.addAction("Edit…", lambda: self.editRequested.emit(index))
        remove = menu.addAction("Remove", lambda: self.removeRequested.emit(index))
        remove.setEnabled(len(self._entries) > 1)
        self._menu = menu
        menu.popup(self.chips[index].mapToGlobal(self.chips[index].rect().bottomLeft()))


def probe_source(source, timeout_ms=4000):
    """Open *source* and read one frame. Returns (ok, message); never the password."""
    import cv2
    from ...config import parse_source
    parsed = parse_source(source)
    if isinstance(parsed, str) and "://" in parsed:
        cap = cv2.VideoCapture(parsed, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms])
    else:
        cap = cv2.VideoCapture(parsed)
    try:
        if not cap.isOpened():
            return False, "Could not connect. Check the address, user and password."
        ok, frame = cap.read()
        if not ok or frame is None:
            return False, "Connected, but no video arrived."
        return True, f"Connected · {frame.shape[1]} × {frame.shape[0]}"
    except Exception:
        return False, "Could not read from this camera."
    finally:
        cap.release()


class CameraDialog(QDialog):
    """Add or edit one camera: a name, a source and a connection test."""

    _probed = pyqtSignal(int, bool, str)

    def __init__(self, entry=None, taken_names=(), theme="dark", parent=None, probe=probe_source):
        super().__init__(parent)
        self.setObjectName("cameraDialog")
        self.setWindowTitle("Edit camera" if entry else "Add camera")
        self._taken = {name.casefold() for name in taken_names}
        self._probe, self._probe_id = probe, 0
        self._theme = theme
        entry = entry or {"name": "", "source": ""}
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel(self.windowTitle())
        title.setObjectName("dialogTitle")
        title.setStyleSheet("font-size: 17px; font-weight: 700;")
        layout.addWidget(title)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name = QLineEdit(entry["name"])
        self.name.setPlaceholderText("Front door")
        self.name.setMaxLength(60)
        form.addRow("Name", self.name)
        self.kind = QComboBox()
        self.kind.addItem("Network camera (RTSP/HTTP)", "url")
        self.kind.addItem("Webcam / device index", "device")
        self.kind.addItem("Video file", "file")
        form.addRow("Type", self.kind)
        self.source = QLineEdit(entry["source"])
        self.source.setMinimumWidth(360)
        self.reveal = self.source.addAction(make_icon("eye", 18, palette(theme)["muted"]),
                                            QLineEdit.ActionPosition.TrailingPosition)
        self.reveal.setCheckable(True)
        self.reveal.setToolTip("Show or hide the address (it may contain a password)")
        self.reveal.toggled.connect(self._sync_echo)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source, 1)
        self.browse = QPushButton("Choose file")
        self.browse.clicked.connect(self._browse)
        source_row.addWidget(self.browse)
        form.addRow("Source", source_row)
        self.hint = QLabel("")
        self.hint.setObjectName("screenSubtitle")
        self.hint.setWordWrap(True)
        form.addRow("", self.hint)
        layout.addLayout(form)
        test_row = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test)
        test_row.addWidget(self.test_button)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        test_row.addWidget(self.result, 1)
        layout.addLayout(test_row)
        self.error = QLabel("")
        self.error.setStyleSheet(f"color: {palette(theme)['danger']};")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._probed.connect(self._show_probe)
        index = self.kind.findData(source_kind(entry["source"]) if entry["source"] else "url")
        self.kind.setCurrentIndex(max(0, index))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.source.textEdited.connect(lambda _text: self._clear_result())
        self._kind_changed()
        self.setMinimumWidth(520)

    def _kind_changed(self):
        kind = self.kind.currentData()
        self.browse.setVisible(kind == "file")
        self.reveal.setVisible(kind == "url")
        self.test_button.setVisible(kind != "device")
        self.source.setPlaceholderText({"url": "rtsp://user:password@192.168.1.64:554/Streaming/Channels/101",
                                        "device": "0", "file": "/path/to/video.mp4"}[kind])
        self.hint.setText({"url": "Saved only in settings.json on this computer; the password is never shown "
                                  "in the monitor or written to the log.",
                           "device": "0 is the built-in camera; 1, 2… are other USB cameras.",
                           "file": "Plays the video as if it were a camera; useful for testing."}[kind])
        self._sync_echo()
        self._clear_result()

    def _sync_echo(self):
        hidden = self.kind.currentData() == "url" and not self.reveal.isChecked()
        self.source.setEchoMode(QLineEdit.EchoMode.Password if hidden else QLineEdit.EchoMode.Normal)
        self.reveal.setIcon(make_icon("eye-off" if self.reveal.isChecked() else "eye", 18,
                                      palette(self._theme)["muted"]))

    def _browse(self):
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose a video file", self.source.text(),
                                                "Video files (*.mp4 *.mov *.avi *.mkv *.m4v);;All files (*)")
        if chosen:
            self.source.setText(chosen)

    def _clear_result(self):
        self._probe_id += 1  # A test still running reports into nothing.
        self.result.setText("")
        self.test_button.setEnabled(True)

    def _test(self):
        problem = camera_problem(self.name.text() or "Camera", self.source.text(), self.kind.currentData())
        if problem:
            self._show_probe(self._probe_id, False, problem.split(": ", 1)[-1])
            return
        self._probe_id += 1
        probe_id, source = self._probe_id, self.source.text().strip()
        self.test_button.setEnabled(False)
        self.result.setStyleSheet(f"color: {palette(self._theme)['muted']};")
        self.result.setText("Connecting…")

        def work():  # Off the GUI thread: an RTSP open can take seconds.
            ok, message = self._probe(source)
            try:
                self._probed.emit(probe_id, ok, message)
            except RuntimeError:
                pass  # Dialog already closed.
        threading.Thread(target=work, name="camera-test", daemon=True).start()

    def _show_probe(self, probe_id, ok, message):
        if probe_id != self._probe_id:
            return
        self.test_button.setEnabled(True)
        self.result.setStyleSheet(f"color: {palette(self._theme)['success' if ok else 'danger']};")
        self.result.setText(("✓ " if ok else "✕ ") + message)

    def values(self):
        return {"name": self.name.text().strip(), "source": self.source.text().strip()}

    def _accept(self):
        entry = self.values()
        problem = ("Give the camera a name" if not entry["name"] else
                   "Another camera already has this name" if entry["name"].casefold() in self._taken else
                   camera_problem(entry["name"], entry["source"], self.kind.currentData()))
        if problem:
            self.error.setText(problem)
            self.error.show()
            return
        self.accept()
