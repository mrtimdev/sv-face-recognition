"""Choose the SV HRM server: Automatic, Online or Office, with a live connection check.

The sign-in form only switches between the saved addresses. Editing them is for Settings,
behind a signed-in account (see ``hrm_auth``); ``editable`` adds those fields.
"""
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QButtonGroup, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QVBoxLayout, QWidget)

from ... import hrm_servers
from ...hrm_sync import default_base_url, plain_http_warning
from ..threads import start_detached
from .form import repolish


class ServerCheckWorker(QThread):
    """Runs one ``hrm_servers.resolve`` off the GUI thread."""

    checked = pyqtSignal(int, object)   # token, Connection

    def __init__(self, token, choice):
        super().__init__()
        self._token = token
        self._choice = choice

    def run(self):
        self.checked.emit(self._token, hrm_servers.resolve(self._choice))


class ServerPicker(QWidget):
    modeChanged = pyqtSignal(str)   # The user picked another mode.

    def __init__(self, editable=False, parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        self._mode = hrm_servers.AUTO
        self._token = 0
        self._worker = None
        self._checked = None       # The choice the status line describes.
        self._saved_online = ""    # Keep addresses this form doesn't show when it has no fields.
        self._saved_office = ""
        self.online_edit = self.office_edit = None

        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        bar = QFrame()
        bar.setObjectName("segmentBar")
        segments = QHBoxLayout(bar)
        segments.setContentsMargins(3, 3, 3, 3)
        segments.setSpacing(2)
        self.buttons = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for mode in hrm_servers.MODES:
            button = QPushButton(hrm_servers.LABELS[mode])
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(hrm_servers.DESCRIPTIONS[mode])
            button.clicked.connect(lambda _checked, chosen=mode: self._pick(chosen))
            self._group.addButton(button)
            self.buttons[mode] = button
            segments.addWidget(button, 1)
        column.addWidget(bar)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(3, 0, 3, 0)
        status_row.setSpacing(8)
        self.status = QLabel("")
        self.status.setObjectName("authStatus")
        status_row.addWidget(self.status, 1)
        self.check_button = QPushButton("Check again")
        self.check_button.setObjectName("linkButton")
        self.check_button.setAutoDefault(False)
        self.check_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_button.clicked.connect(self.check)
        status_row.addWidget(self.check_button)
        column.addLayout(status_row)
        self.detail = QLabel("")
        self.detail.setObjectName("authFooter")
        self.detail.setContentsMargins(3, 0, 3, 0)
        self.detail.setWordWrap(True)
        column.addWidget(self.detail)
        if editable:
            self._add_address_fields(column)
        self.buttons[self._mode].setChecked(True)

    def _add_address_fields(self, column):
        form = QFormLayout()
        form.setContentsMargins(0, 6, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.online_edit = QLineEdit()
        self.online_edit.setPlaceholderText(hrm_servers.DEFAULT_ONLINE_URL)
        self.office_edit = QLineEdit()
        self.office_edit.setPlaceholderText(default_base_url())
        form.addRow("Internet address", self.online_edit)
        form.addRow("Office network address", self.office_edit)
        column.addLayout(form)
        self.warning = QLabel("")
        self.warning.setObjectName("authFooter")
        self.warning.setWordWrap(True)
        self.warning.hide()
        column.addWidget(self.warning)
        note = QLabel("Both must reach the same server. Save the settings to keep a change.")
        note.setObjectName("authFooter")
        note.setWordWrap(True)
        column.addWidget(note)
        for edit in (self.online_edit, self.office_edit):
            edit.textChanged.connect(self._addresses_edited)
            edit.editingFinished.connect(self._check_if_changed)

    # --- value ------------------------------------------------------------
    def profile(self):
        if self.online_edit is None:
            return hrm_servers.Profile(self._mode, self._saved_online, self._saved_office)
        return hrm_servers.Profile(self._mode, hrm_servers.normalize_url(self.online_edit.text()),
                                   hrm_servers.normalize_url(self.office_edit.text()))

    def set_profile(self, choice):
        self._mode = choice.mode
        self._saved_online, self._saved_office = choice.online, choice.office
        self.buttons[choice.mode].setChecked(True)
        if self.online_edit is not None:
            self.online_edit.setText(choice.online)
            self.office_edit.setText(choice.office)
        self._check_if_changed()

    def address_problem(self):
        """The first address that can't be used; "" when both can."""
        if self.online_edit is None:
            return ""
        for label, edit in (("Internet", self.online_edit), ("Office network", self.office_edit)):
            problem = hrm_servers.address_problem(edit.text())
            if problem:
                return f"{label} address: {problem}"
        return ""

    # --- behaviour --------------------------------------------------------
    def _pick(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        self.check()
        self.modeChanged.emit(mode)

    def _addresses_edited(self):
        warnings = [plain_http_warning(hrm_servers.normalize_url(edit.text()))
                    for edit in (self.online_edit, self.office_edit)]
        message = next((text for text in warnings if text), "")
        self.warning.setText(message)
        self.warning.setVisible(bool(message))

    def _check_if_changed(self):
        if self.isVisible() and self._checked != self.profile():
            self.check()

    def showEvent(self, event):
        super().showEvent(event)
        self._check_if_changed()

    def check(self):
        problem = self.address_problem()
        if problem:
            self._token += 1
            self._checked = None
            self._show_status("Can't connect", problem, "bad")
            self.check_button.setEnabled(True)
            return
        self._token += 1
        self._checked = self.profile()
        self.check_button.setEnabled(False)
        self._show_status("Checking the connection…", hrm_servers.DESCRIPTIONS[self._mode])
        worker = ServerCheckWorker(self._token, self._checked)
        worker.checked.connect(self._on_checked)
        # Parked, not owned: closing the form mid-check must not end the thread.
        self._worker = worker
        start_detached(worker)

    def _on_checked(self, token, connection):
        if token != self._token:
            return
        self.check_button.setEnabled(True)
        headline, detail = hrm_servers.status_lines(connection)
        self._show_status(headline, detail, "ok" if connection.ok else "bad")

    def _show_status(self, headline, detail, tone=None):
        self.status.setText(headline)
        repolish(self.status, "tone", tone)
        self.detail.setText(detail)
