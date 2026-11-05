"""Choose the SV HRM server: Automatic, Online or Office, with a live connection check.

Settings edits both addresses (``editable``). The sign-in form switches modes and takes an
office address only (``office_input``): one on the local network that answers as SV HRM.
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
    officeChanged = pyqtSignal(str)  # A typed office address answered as SV HRM and is now in use.

    def __init__(self, editable=False, office_input=False, parent=None):
        super().__init__(parent)
        self.setObjectName("plain")
        self._mode = hrm_servers.AUTO
        self._token = 0
        self._worker = None
        self._checked = None       # The choice the status line describes.
        self._saved_online = ""    # Keep addresses this form doesn't show when it has no fields.
        self._saved_office = ""
        self.online_edit = self.office_edit = self.office_input = None
        self._trying_office = None  # A typed address waiting on its check.

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
        if office_input:
            self._add_office_input(column)

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

    def _add_office_input(self, column):
        self.office_row = QWidget()
        self.office_row.setObjectName("plain")
        row = QHBoxLayout(self.office_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.office_input = QLineEdit()
        self.office_input.setPlaceholderText(default_base_url())
        self.office_input.setToolTip("The SV HRM address on the office network.")
        self.office_input.returnPressed.connect(self.use_office)
        row.addWidget(self.office_input, 1)
        self.use_button = QPushButton("Use")
        self.use_button.setObjectName("softButton")
        self.use_button.setAutoDefault(False)
        self.use_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.use_button.clicked.connect(self.use_office)
        row.addWidget(self.use_button)
        column.addWidget(self.office_row)
        self.office_row.setVisible(self._mode == hrm_servers.OFFICE)

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
        if self.office_input is not None:
            self.office_input.setText(choice.office)
            self.office_row.setVisible(choice.mode == hrm_servers.OFFICE)
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

    def pending_office(self):
        """A typed office address that isn't in use yet; "" when there is none."""
        if self.office_input is None or self._mode != hrm_servers.OFFICE:
            return ""
        typed = hrm_servers.normalize_url(self.office_input.text())
        return typed or " " if typed != self._saved_office else ""

    # --- behaviour --------------------------------------------------------
    def _pick(self, mode):
        if mode == self._mode:
            return
        self._mode = mode
        if self.office_input is not None:
            self.office_input.setText(self._saved_office)
            self.office_row.setVisible(mode == hrm_servers.OFFICE)
        self.check()
        self.modeChanged.emit(mode)

    def use_office(self):
        """Check a typed office address; it is only kept once it answers as SV HRM."""
        if not self.check_button.isEnabled() and self._trying_office is not None:
            return
        address = hrm_servers.normalize_url(self.office_input.text())
        problem = hrm_servers.office_address_problem(address)
        if problem:
            self._token += 1
            self._trying_office = None
            self._set_checking(False)
            self._show_status("Can't use this address", problem, "bad")
            return
        self._start_check(hrm_servers.Profile(hrm_servers.OFFICE, self._saved_online, address), address)

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
            self._trying_office = None
            self._show_status("Can't connect", problem, "bad")
            self._set_checking(False)
            return
        self._start_check(self.profile())

    def _set_checking(self, checking):
        self.check_button.setEnabled(not checking)
        if self.office_input is not None:
            self.use_button.setEnabled(not checking)

    def _start_check(self, choice, office=None):
        self._token += 1
        self._checked = choice
        self._trying_office = office
        self._set_checking(True)
        self._show_status("Checking the connection…", hrm_servers.DESCRIPTIONS[choice.mode])
        worker = ServerCheckWorker(self._token, choice)
        worker.checked.connect(self._on_checked)
        # Parked, not owned: closing the form mid-check must not end the thread.
        self._worker = worker
        start_detached(worker)

    def _on_checked(self, token, connection):
        if token != self._token:
            return
        self._set_checking(False)
        office, self._trying_office = self._trying_office, None
        if office is not None and not connection.ok:
            self._checked = None
        elif office is not None:
            self._saved_office = office
            self.office_input.setText(office)
            self.officeChanged.emit(office)
        headline, detail = hrm_servers.status_lines(connection)
        self._show_status(headline, detail, "ok" if connection.ok else "bad")

    def _show_status(self, headline, detail, tone=None):
        self.status.setText(headline)
        repolish(self.status, "tone", tone)
        self.detail.setText(detail)
