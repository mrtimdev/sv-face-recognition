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
from ..storage import read_image

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
        image = read_image(path)
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

    _batchReady = pyqtSignal(object)    # generation, destination, config, messages
    statusChanged = pyqtSignal(str)

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
        self._generation = 0
        self._batch_destination = ""
        self._batch_config = None
        self._in_flight = set()
        self._closed = False
        self.status = "Waiting for HRM connection"
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
        if self._closed or not self._link.connected or self._reading or self._in_flight:
            return
        self._reading = True
        self._set_status("Checking saved attendance")
        self._submit(("read", None, self._generation, self._link.destination))

    def close(self):
        self._closed = True
        self._tick.stop()
        self._ack_timer.stop()
        if self._thread is not None:
            self._commands.put(None)
            self._thread.join(3)

    def _submit(self, command, config=None):
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, name="hrm-attendance-uplink", daemon=True)
            self._thread.start()
        self._commands.put((*command, self._config_getter() if config is None else config))

    def _on_connected(self, connected):
        if self._closed:
            return
        # Unanswered messages are sent again on the next read.
        self._in_flight.clear()
        self._generation += 1
        self._reading = False
        self._ack_timer.stop()
        if connected:
            self.kick()
        else:
            self._set_status("Offline · saved attendance waits locally")

    def _set_status(self, text):
        if self.status != text:
            self.status = text
            self.statusChanged.emit(text)

    def _on_batch(self, batch):
        if self._closed:
            return
        generation, destination, config, messages = batch
        if generation != self._generation or destination != self._link.destination:
            return
        self._reading = False
        if config != self._config_getter():
            return  # Database/settings changed while this batch was being read.
        if not self._link.connected:
            return
        if messages is None:
            self._set_status("Local sync queue unavailable · retrying automatically")
            return
        self._batch_destination = destination
        self._batch_config = config
        for event_id, text in messages:
            if self._link.send_text(text):
                self._in_flight.add(event_id)
        if self._in_flight:
            self._set_status(f"Waiting for HRM · {len(self._in_flight)} event(s)")
            self._ack_timer.start()
        else:
            self._set_status("No pending attendance for this server" if not messages else "Send delayed · retrying automatically")

    def _on_ack(self, event_id, result):
        if self._closed or event_id not in self._in_flight:
            return
        self._in_flight.discard(event_id)
        if result == "rejected":
            self._set_status("HRM rejected attendance · review Attendance Report")
            logging.warning("HRM refused face attendance event %s; it won't be sent again", event_id)
        if result in DONE_RESULTS:
            self._submit(("mark", (event_id, result), self._generation, self._batch_destination),
                         config=self._batch_config)
        if not self._in_flight:
            self._ack_timer.stop()
            if result in DONE_RESULTS:
                self.kick()     # More may be waiting. After a "retry", wait for the next tick.

    def _on_ack_timeout(self):
        logging.info("HRM didn't acknowledge %d face attendance event(s); sending again", len(self._in_flight))
        self._in_flight.clear()
        self.kick()

    # --- worker thread --------------------------------------------------------

    def _work(self):
        repository = None
        repository_config = None
        while True:
            command = self._commands.get()
            if command is None:
                break
            kind, value, generation, destination, config = command
            try:
                if repository is not None and config != repository_config:
                    self._close(repository)
                    repository = None
                if repository is None:
                    repository = self._repository_factory(config)
                    repository_config = config
                if kind == "mark":
                    event_id, result = value
                    repository.resolve_delivery(event_id, result, destination=destination)
                else:
                    rows = repository.pending_outbox(BATCH_SIZE, destination=destination)
                    self._batchReady.emit((generation, destination, config,
                        [(row["eventId"], build_message(row, self._device_name)) for row in rows]))
            except RuntimeError as exc:
                if "wrapped C/C++ object" in str(exc):
                    break   # The window closed meanwhile.
                repository = self._on_failure(repository, kind, exc, generation, destination, config)
            except Exception as exc:
                repository = self._on_failure(repository, kind, exc, generation, destination, config)
        self._close(repository)

    def _on_failure(self, repository, kind, exc, generation, destination, config):
        logging.warning("Face attendance upload couldn't use the local database: %s", exc)
        self._close(repository)
        if kind == "read":
            try:
                self._batchReady.emit((generation, destination, config, None))
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
