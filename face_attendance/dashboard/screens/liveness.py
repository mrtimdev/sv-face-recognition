"""Live Monitor › More › Liveness test: the bundled liveness check on sample photos.

Runs the same detector and anti-spoof models as Live Monitor on three public
sample photos - a real face, a printed photo and a phone screen - so an
installation proves its liveness check works before anyone is enrolled.  The
window also adds or removes the optional demo employee made from the
real-face sample.
"""
from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ... import samples
from ...storage import read_image
from ..access import guard
from ..bridge import to_pixmap
from ..threads import settle
from ..widgets import IconTile, StatusPill
from ..widgets.form import repolish
from ..widgets.overlay import WindowOverlay

PHOTO_SIZE = (150, 200)


class _CheckWorker(QThread):
    done = pyqtSignal(object, str)          # SampleResult list, error text

    def __init__(self, detection_scale):
        super().__init__()
        self._scale = detection_scale

    def run(self):
        try:
            results = samples.check_samples(detection_scale=self._scale)
        except Exception as exc:            # a broken model must be reported, not crash the thread
            self.done.emit([], str(exc) or exc.__class__.__name__)
            return
        self.done.emit(results, "")


class _SampleTile(QFrame):
    """One sample photo with what it should do and what the check decided."""

    def __init__(self, sample, theme):
        super().__init__()
        self.sample = sample
        self.setObjectName("banner")
        self.setProperty("tone", "idle")
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 12, 12, 12)
        column.setSpacing(4)
        self.photo = QLabel()
        self.photo.setFixedSize(*PHOTO_SIZE)
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image = read_image(sample.path)
        if image is not None:
            self.photo.setPixmap(to_pixmap(image).scaled(
                *PHOTO_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        column.addWidget(self.photo, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(6)
        title = QLabel(sample.title)
        title.setObjectName("cellTitle")
        expected = QLabel("Should pass" if sample.expected_live else "Should be blocked")
        expected.setObjectName("fieldHelp")
        self.result = StatusPill("Checking…", "idle", dot=True, theme=theme)
        for widget in (title, expected):
            column.addWidget(widget, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(4)
        column.addWidget(self.result, 0, Qt.AlignmentFlag.AlignHCenter)

    def show_result(self, result):
        if result is None:
            self.result.set_status("Checking…", "idle")
        elif result.error:
            self.result.set_status("Couldn't check", "bad")
            self.result.setToolTip(result.error)
        else:
            verdict = "Passed" if result.judged_live else "Blocked"
            self.result.set_status(f"{verdict} • {result.live_score:.2f}",
                                   "ok" if result.correct else "bad")
            self.result.setToolTip("Live score from the stricter of the two models; "
                                   "a face passes at 0.90 or more.")

    def set_theme(self, theme):
        self.result.set_theme(theme)


class LivenessTestDialog(WindowOverlay):
    CARD_WIDTH = 640

    def __init__(self, settings, engine, theme="light", parent=None):
        super().__init__(theme, parent, dismissible=True, title="Liveness test")
        self.settings = settings
        self.engine = engine
        self.results = []
        self._worker = None
        column = self.column

        top = QHBoxLayout()
        top.setSpacing(14)
        top.addWidget(IconTile("shield", tone="blue", size=48, theme=theme), 0, Qt.AlignmentFlag.AlignTop)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        title = QLabel("Liveness test")
        title.setObjectName("dialogTitle")
        subtitle = QLabel("Runs this computer's liveness check on three sample photos. "
                          "No camera or enrolled employees needed.")
        subtitle.setObjectName("dialogSubtitle")
        subtitle.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(subtitle)
        top.addLayout(heading, 1)
        column.addLayout(top)
        column.addSpacing(16)

        self.verdict = QFrame()
        self.verdict.setObjectName("banner")
        self.verdict.setProperty("tone", "idle")
        verdict = QVBoxLayout(self.verdict)
        verdict.setContentsMargins(12, 8, 12, 8)
        verdict.setSpacing(2)
        self.verdict_title = QLabel("")
        self.verdict_title.setObjectName("bannerTitle")
        self.verdict_body = QLabel("")
        self.verdict_body.setObjectName("bannerBody")
        self.verdict_body.setWordWrap(True)
        verdict.addWidget(self.verdict_title)
        verdict.addWidget(self.verdict_body)
        column.addWidget(self.verdict)
        column.addSpacing(12)

        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tiles = [_SampleTile(sample, theme) for sample in samples.SAMPLES]
        for tile in self.tiles:
            tiles.addWidget(tile, 1)
        column.addLayout(tiles)
        column.addSpacing(18)

        demo_title = QLabel("Demo employee")
        demo_title.setObjectName("formSectionTitle")
        demo_hint = QLabel(f"Enrolls the real-face sample as “{samples.DEMO_LABEL}” "
                           f"({samples.DEMO_EMPLOYEE_ID}). Show any sample photo to the camera on a "
                           "phone or on paper: Live Monitor recognizes the demo employee but blocks "
                           "attendance as a photo. Remove it when you're done.")
        demo_hint.setObjectName("fieldHelp")
        demo_hint.setWordWrap(True)
        column.addWidget(demo_title)
        column.addSpacing(4)
        column.addWidget(demo_hint)
        column.addSpacing(10)
        demo = QHBoxLayout()
        demo.setSpacing(8)
        self.demo_button = QPushButton("")
        self.demo_button.clicked.connect(self._toggle_demo)
        self.photos_button = QPushButton("Show sample photos")
        self.photos_button.setObjectName("ghostButton")
        self.photos_button.clicked.connect(self._show_photos)
        demo.addWidget(self.demo_button)
        demo.addWidget(self.photos_button)
        demo.addStretch(1)
        column.addLayout(demo)
        self.demo_status = QLabel("")
        self.demo_status.setObjectName("fieldHelp")
        self.demo_status.setWordWrap(True)
        self.demo_status.hide()
        column.addSpacing(6)
        column.addWidget(self.demo_status)
        column.addSpacing(20)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.again_button = QPushButton("Run again")
        self.again_button.setObjectName("controlButton")
        self.again_button.clicked.connect(self.run_check)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("primary")
        self.close_button.clicked.connect(self.accept)
        footer.addStretch(1)
        footer.addWidget(self.again_button)
        footer.addWidget(self.close_button)
        column.addLayout(footer)
        for button in (self.demo_button, self.photos_button, self.again_button, self.close_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sync_demo()
        self.run_check()

    # --- the check -----------------------------------------------------------
    def run_check(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self._set_verdict("idle", "Checking the sample photos…",
                          "Using the face detector and liveness models installed with this app.")
        for tile in self.tiles:
            tile.show_result(None)
        self.again_button.setEnabled(False)
        try:
            scale = float(self.settings.detection_scale)
        except (TypeError, ValueError):
            scale = 0.5
        self._worker = _CheckWorker(scale)
        self._worker.done.connect(self._on_checked)
        self._worker.start()

    def is_busy(self):
        return self._worker is not None and self._worker.isRunning()

    def _on_checked(self, results, error):
        self.again_button.setEnabled(True)
        self.results = results
        if error:
            self._set_verdict("bad", "The liveness check couldn't run", error)
            return
        for tile, result in zip(self.tiles, results):
            tile.show_result(result)
        wrong = [result for result in results if not result.correct]
        if not wrong:
            self._set_verdict("ok", "The liveness check works on this computer",
                              "The real face passed and both photos were blocked, as expected.")
            return
        details = []
        for result in wrong:
            if result.error:
                details.append(f"{result.sample.title}: {result.error}")
            else:
                outcome = "passed" if result.judged_live else "was blocked"
                details.append(f"{result.sample.title} {outcome} (score {result.live_score:.2f})")
        self._set_verdict("bad", "The liveness check didn't behave as expected",
                          "; ".join(details) + ". Attendance stays blocked whenever the check fails.")

    def _set_verdict(self, tone, title, body):
        repolish(self.verdict, "tone", tone)
        self.verdict_title.setText(title)
        self.verdict_body.setText(body)

    # --- demo employee ----------------------------------------------------------
    def _sync_demo(self):
        present = samples.has_demo_employee(self.settings.encodings_path)
        self.demo_button.setText("Remove demo employee" if present else "Add demo employee")
        self.demo_button.setObjectName("controlButton" if present else "primary")
        self.demo_button.style().unpolish(self.demo_button)
        self.demo_button.style().polish(self.demo_button)

    def _toggle_demo(self):
        present = samples.has_demo_employee(self.settings.encodings_path)
        action = "remove the demo employee" if present else "add the demo employee"
        if not guard(self, "manage_employees", action):
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            if present:
                samples.remove_demo_employee(self.settings.encodings_path, self.settings.employees_path)
                message = "Removed the demo employee. Their attendance history, if any, is kept."
            else:
                outcome = samples.add_demo_employee(self.settings.encodings_path,
                                                    self.settings.employees_path)
                message = ("Added the demo employee. Show a sample photo to the camera in Live Monitor."
                           if outcome.ok else f"Couldn't add the demo employee: {outcome.message}")
        except (OSError, ValueError) as exc:
            message = f"Couldn't {action}: {exc}"
        finally:
            QApplication.restoreOverrideCursor()
        if self.engine is not None:
            self.engine.reload_catalog()
        self.demo_status.setText(message)
        self.demo_status.show()
        self._sync_demo()

    def _show_photos(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(samples.SAMPLES_DIR)))

    def done(self, result):
        settle(self._worker)
        super().done(result)
