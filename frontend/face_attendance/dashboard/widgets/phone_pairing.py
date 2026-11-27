"""Pair a phone on the same Wi-Fi: show a 6-digit code the phone app types in.

The phone finds this computer itself (Find Desktop on Wi-Fi), so nobody types an address or a
key; the code proves the phone is in the room. See machine_server.py and web/machine_api.py.
"""
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout


class PhonePairingDialog(QDialog):
    def __init__(self, link, parent=None):
        super().__init__(parent)
        self.link = link
        self.setWindowTitle("Pair a phone")
        self.setMinimumWidth(380)
        self._paired_before = len(link.pairing.paired_devices)

        layout = QVBoxLayout(self)
        steps = QLabel("On the phone, in the SV Face Angles app:\n"
                       "1. Open server settings (⚙) and tap Find Desktop on Wi-Fi.\n"
                       f"2. Choose “{link.identity()[1]}”.\n"
                       "3. Enter this code:")
        steps.setWordWrap(True)
        layout.addWidget(steps)

        self.code = QLabel()
        font = QFont()
        font.setPointSize(34)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 6)
        self.code.setFont(font)
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.code)

        self.status = QLabel()
        self.status.setObjectName("screenSubtitle")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        address = QLabel(f"The phone must be on the same Wi-Fi. If it can't find this computer, "
                         f"enter {link.phone_address()} in its server settings instead.")
        address.setObjectName("screenSubtitle")
        address.setWordWrap(True)
        address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(address)

        buttons = QDialogButtonBox()
        self.new_code = buttons.addButton("New code", QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        self.new_code.clicked.connect(self._start)
        layout.addWidget(buttons)

        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self._start()

    def _start(self):
        code = self.link.pairing.start()
        self.code.setText(f"{code[:3]} {code[3:]}")
        self.timer.start()
        self._tick()

    def _tick(self):
        devices = self.link.pairing.paired_devices
        if len(devices) > self._paired_before:
            self.timer.stop()
            self.code.setText("✓")
            self.status.setText(f"Paired with {devices[-1]}. It can register face angles now.")
            self.new_code.setText("Pair another phone")
            self._paired_before = len(devices)
            return
        left = self.link.pairing.seconds_left()
        if not self.link.pairing.active:
            self.timer.stop()
            self.code.setText("— — —")
            self.status.setText("This code has ended (time up or too many wrong tries). Choose New code.")
            return
        self.status.setText(f"Waiting for the phone… the code works for {left // 60}:{left % 60:02d} more.")

    def done(self, result):
        self.timer.stop()
        if self.code.text() != "✓":
            self.link.pairing.cancel()     # An unused code doesn't outlive the dialog.
        super().done(result)
