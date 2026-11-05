"""Sends Live Monitor check-ins to SV HRM over the live socket, in the background.

Every check-in is saved locally first, together with an outbox row in the same transaction. This
sends outbox rows over the socket and only marks one as sent when HRM acknowledges it, so nothing
is lost while HRM is offline, and HRM's unique event ID makes sending one twice harmless.
"""
import base64
import json
import logging
import platform
import queue
import threading

import cv2
from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from ..repository import AttendanceRepository

ATTENDANCE_LOG = "attendance.log"
BATCH_SIZE = 20
TICK_MS = 5_000             # Also catches anything a missed wake-up left behind.
ACK_TIMEOUT_MS = 30_000
PHOTO_MAX_SIDE = 480
PHOTO_QUALITY = 80
DONE_RESULTS = ("saved", "duplicate", "rejected")   # "retry" stays in the outbox.


def photo_text(path):
    """The check-in photo as base64 JPEG, small enough for the socket; None when there is none."""
    if not path:
        return None
    try:
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            return None
        longest = max(image.shape[:2])
        if longest > PHOTO_MAX_SIDE:
            scale = PHOTO_MAX_SIDE / longest
            image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, PHOTO_QUALITY])
    except (OSError, ValueError, cv2.error) as exc:
        logging.warning("Could not read a check-in photo: %s", exc)
        return None
    return base64.b64encode(data.tobytes()).decode("ascii") if ok else None


def build_message(payload, device_name):
    """The socket message for one outbox row."""
    message = {key: payload.get(key) for key in ("eventId", "employeeId", "employeeName", "capturedAt", "recordedAt",
                                                 "verifiedPresenceSeconds", "matchDistance", "liveScore")}
    message.update(type=ATTENDANCE_LOG, deviceName=device_name, snapshotJpeg=photo_text(payload.get("snapshotPath")))
    return json.dumps(message, ensure_ascii=False)


class AttendanceUplink(QObject):
    """Lives on the GUI thread; the database and photo work runs on its own thread."""

    _batchReady = pyqtSignal(object)    # [(event ID, message text)]

    def __init__(self, link, config_getter, parent=None, repository_factory=AttendanceRepository,
                 tick_ms=TICK_MS, ack_timeout_ms=ACK_TIMEOUT_MS, device_name=None):
        super().__init__(parent)
        self._link = link
        self._config_getter = config_getter
        self._repository_factory = repository_factory
        self._device_name = device_name or platform.node()
        self._commands = queue.Queue()
        self._thread = None
        self._reading = False
        self._in_flight = set()
        self._batchReady.connect(self._on_batch)
        link.connectedChanged.connect(self._on_connected)
        link.attendanceAck.connect(self._on_ack)
        self._tick = QTimer(self, interval=tick_ms)
        self._tick.timeout.connect(self.kick)
        self._tick.start()
        self._ack_timer = QTimer(self, singleShot=True, interval=ack_timeout_ms)
        self._ack_timer.timeout.connect(self._on_ack_timeout)

    def kick(self):
        """Send whatever is waiting. Safe to call at any time, such as right after a check-in."""
        if not self._link.connected or self._reading or self._in_flight:
            return
        self._reading = True
        self._submit(("read", None))

    def close(self):
        self._tick.stop()
        self._ack_timer.stop()
        if self._thread is not None:
            self._commands.put(None)
            self._thread.join(3)

    def _submit(self, command):
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="hrm-attendance-uplink", daemon=True)
            self._thread.start()
        self._commands.put(command)

    def _on_connected(self, connected):
        # Unanswered messages are sent again on the next read.
        self._in_flight.clear()
        self._ack_timer.stop()
        if connected:
            self.kick()

    def _on_batch(self, messages):
        self._reading = False
        if not self._link.connected:
            return
        for event_id, text in messages:
            if self._link.send_text(text):
                self._in_flight.add(event_id)
        if self._in_flight:
            self._ack_timer.start()

    def _on_ack(self, event_id, result):
        if event_id not in self._in_flight:
            return
        self._in_flight.discard(event_id)
        if result == "rejected":
            logging.warning("HRM refused face attendance event %s; it won't be sent again", event_id)
        if result in DONE_RESULTS:
            self._submit(("mark", event_id))
        if not self._in_flight:
            self._ack_timer.stop()
            if result in DONE_RESULTS:
                self.kick()     # More may be waiting. After a "retry", wait for the next tick.

    def _on_ack_timeout(self):
        logging.info("HRM didn't acknowledge %d face attendance event(s); sending again", len(self._in_flight))
        self._in_flight.clear()

    # --- worker thread --------------------------------------------------------

    def _work(self):
        repository = None
        while True:
            command = self._commands.get()
            if command is None:
                break
            kind, event_id = command
            try:
                if repository is None:
                    repository = self._repository_factory(self._config_getter())
                if kind == "mark":
                    repository.mark_synced(event_id)
                else:
                    rows = repository.pending_outbox(BATCH_SIZE)
                    self._batchReady.emit([(row["eventId"], build_message(row, self._device_name)) for row in rows])
            except RuntimeError as exc:
                if "wrapped C/C++ object" in str(exc):
                    break   # The window closed meanwhile.
                repository = self._on_failure(repository, kind, exc)
            except Exception as exc:
                repository = self._on_failure(repository, kind, exc)
        self._close(repository)

    def _on_failure(self, repository, kind, exc):
        logging.warning("Face attendance upload couldn't use the local database: %s", exc)
        self._close(repository)
        if kind == "read":
            try:
                self._batchReady.emit([])   # Stops waiting; the next tick tries again.
            except RuntimeError:
                pass
        return None

    @staticmethod
    def _close(repository):
        if repository is not None:
            try:
                repository.close()
            except Exception:
                logging.debug("Closing the upload's database connection failed", exc_info=True)
