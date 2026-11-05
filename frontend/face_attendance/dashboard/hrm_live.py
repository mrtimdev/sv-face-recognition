"""The live connection to SV HRM: hears about employee changes the moment they happen.

HRM sends a small event over a WebSocket (see svhrm's docs/face-angle-api.md). An event only says
which employee changed, so the app then reads the details through the normal API. If the socket
is down, the Employees screen's periodic refresh still keeps the list current, only more slowly.
"""
import json
import logging
import urllib.parse

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkRequest
from PyQt6.QtWebSockets import QWebSocket

EVENTS_PATH = "/api/face-recognition/events"
EMPLOYEE_CHANGED = "employee.changed"
ANGLES_CHANGED = "angles.changed"
PHOTO_CHANGED = "photo.changed"
ANGLES_VERIFIED = "angles.verified"
ATTENDANCE_ACK = "attendance.ack"
MAX_EVENT_CHARS = 4096
SETTLE_MS = 600                 # Several saves in a row become one refresh.
PING_MS = 30_000                # A connection that stops answering is replaced.
RECONNECT_FIRST_MS = 2_000
RECONNECT_MAX_MS = 60_000


def events_url(base_url):
    """The socket address for an HRM address, or "" when it can't be used."""
    parts = urllib.parse.urlsplit((base_url or "").strip().rstrip("/"))
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme)
    if not scheme or not parts.hostname:
        return ""
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path + EVENTS_PATH, "", ""))


def parse_event(text):
    """(type, employee code) for a known event, else None. Whatever else arrives is ignored."""
    if len(text) > MAX_EVENT_CHARS:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") not in (EMPLOYEE_CHANGED, ANGLES_CHANGED, PHOTO_CHANGED, ANGLES_VERIFIED):
        return None
    code = data.get("employeeCode")
    if not isinstance(code, str) or not code.strip():
        return None
    return data["type"], code.strip()


def parse_ack(text):
    """(event ID, result) when HRM is acknowledging a check-in, else None."""
    if len(text) > MAX_EVENT_CHARS:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") != ATTENDANCE_ACK:
        return None
    event_id, result = data.get("eventId"), data.get("result")
    return (event_id, result) if isinstance(event_id, str) and isinstance(result, str) else None


class HrmLiveLink(QObject):
    """Keeps one socket open to HRM and reconnects when it drops. Lives on the GUI thread."""

    employeesChanged = pyqtSignal()      # Refresh the directory. Also sent after each connect,
                                         # because changes made while disconnected aren't replayed.
    anglesChanged = pyqtSignal(str)      # This employee's angle images changed in HRM.
    photoChanged = pyqtSignal(str)       # This employee's profile photo changed in HRM.
    anglesVerified = pyqtSignal(str)     # New face check results for this employee; the images are the same.
    connectedChanged = pyqtSignal(bool)
    attendanceAck = pyqtSignal(str, str)  # Event ID, and HRM's answer: saved, duplicate, rejected or retry.

    def __init__(self, parent=None, settle_ms=SETTLE_MS, ping_ms=PING_MS, reconnect_ms=RECONNECT_FIRST_MS):
        super().__init__(parent)
        self._url, self._key = "", ""
        self._socket = None
        self._connected = False
        self._awaiting_pong = False
        self._reconnect_first = reconnect_ms
        self._backoff = reconnect_ms
        self._settle = QTimer(self, singleShot=True, interval=settle_ms)
        self._settle.timeout.connect(self.employeesChanged)
        self._ping = QTimer(self, interval=ping_ms)
        self._ping.timeout.connect(self._send_ping)
        self._reconnect = QTimer(self, singleShot=True)
        self._reconnect.timeout.connect(self._open)

    @property
    def connected(self):
        return self._connected

    def configure(self, config):
        """Connect with this HRM address and key; a change replaces the connection."""
        url = events_url(config.base_url) if config.configured else ""
        key = config.api_key.strip() if url else ""
        if (url, key) == (self._url, self._key):
            return
        self.stop()
        self._url, self._key = url, key
        self._backoff = self._reconnect_first
        if url:
            self._open()

    def send_text(self, text):
        """Send one message now. False when there is no connection."""
        if not self._connected or self._socket is None:
            return False
        return self._socket.sendTextMessage(text) > 0

    def stop(self):
        self._url, self._key = "", ""
        self._reconnect.stop()
        self._settle.stop()
        self._close_socket()
        self._set_connected(False)

    def _open(self):
        if not self._url or self._socket is not None:
            return
        socket = QWebSocket(parent=self)
        socket.connected.connect(self._on_connected)
        socket.disconnected.connect(lambda: self._on_lost(socket))
        socket.errorOccurred.connect(lambda _error: self._on_lost(socket))
        socket.textMessageReceived.connect(self._on_message)
        socket.pong.connect(self._on_pong)
        self._socket = socket
        request = QNetworkRequest(QUrl(self._url))
        request.setRawHeader(b"X-API-KEY", self._key.encode("utf-8"))
        socket.open(request)

    def _close_socket(self):
        socket, self._socket = self._socket, None
        self._ping.stop()
        if socket is not None:
            # Signals off first, so closing on purpose doesn't schedule a reconnect.
            socket.blockSignals(True)
            socket.abort()
            socket.deleteLater()

    def _on_connected(self):
        self._backoff = self._reconnect_first
        self._awaiting_pong = False
        self._ping.start()
        self._set_connected(True)
        self.employeesChanged.emit()

    def _on_lost(self, socket):
        if socket is not self._socket:
            return  # An old socket reporting late.
        self._close_socket()
        self._set_connected(False)
        if self._url:
            self._reconnect.start(self._backoff)
            self._backoff = min(self._backoff * 2, RECONNECT_MAX_MS)

    def _on_message(self, text):
        ack = parse_ack(text)
        if ack is not None:
            self.attendanceAck.emit(*ack)
            return
        event = parse_event(text)
        if event is None:
            logging.debug("Ignored an unknown HRM live event")
        elif event[0] == EMPLOYEE_CHANGED:
            self._settle.start()
        elif event[0] == PHOTO_CHANGED:
            self.photoChanged.emit(event[1])
        elif event[0] == ANGLES_VERIFIED:
            self.anglesVerified.emit(event[1])
        else:
            self.anglesChanged.emit(event[1])

    def _send_ping(self):
        if self._awaiting_pong:
            logging.info("HRM live connection stopped answering; reconnecting")
            self._on_lost(self._socket)
            return
        self._awaiting_pong = True
        self._socket.ping()

    def _on_pong(self, *_):
        self._awaiting_pong = False

    def _set_connected(self, connected):
        if connected != self._connected:
            self._connected = connected
            logging.info("HRM live updates %s", "connected" if connected else "disconnected")
            self.connectedChanged.emit(connected)
