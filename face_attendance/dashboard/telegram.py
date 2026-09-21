"""Telegram Bot integration for attendance notifications.

Sends attendance capture photos and unknown-face danger alerts to a configured
Telegram chat. All network calls run on a dedicated daemon thread so the Qt
event loop and the engine loop are never blocked.
"""
import io
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import cv2

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


API_BASE = "https://api.telegram.org/bot{token}"
SEND_PHOTO = API_BASE + "/sendPhoto"
SEND_MESSAGE = API_BASE + "/sendMessage"
GET_ME = API_BASE + "/getMe"
TIMEOUT = 15
MAX_QUEUE = 64


@dataclass(frozen=True)
class TelegramJob:
    kind: str                        # "capture" | "unknown" | "test"
    caption: str
    image: Optional[bytes] = None    # JPEG bytes; None for text-only messages


class TelegramService:
    """Thread-safe, non-blocking Telegram sender with graceful degradation."""

    def __init__(self, bot_token: str = "", chat_id: str = "",
                 notify_capture: bool = True, notify_unknown: bool = True,
                 cooldown_unknown_sec: float = 30.0):
        self._token = bot_token.strip()
        self._chat_id = chat_id.strip()
        self._notify_capture = notify_capture
        self._notify_unknown = notify_unknown
        self._cooldown_unknown = cooldown_unknown_sec
        self._last_unknown = float("-inf")
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        if self.configured:
            self._start_worker()

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id and _HAS_REQUESTS)

    def reconfigure(self, bot_token: str, chat_id: str,
                    notify_capture: bool = True, notify_unknown: bool = True):
        with self._lock:
            self._token = bot_token.strip()
            self._chat_id = chat_id.strip()
            self._notify_capture = notify_capture
            self._notify_unknown = notify_unknown
        if self.configured and (self._thread is None or not self._thread.is_alive()):
            self._start_worker()

    def _start_worker(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telegram-sender", daemon=True)
        self._thread.start()

    # -- public API (called from any thread) --------------------------------

    def send_capture(self, employee_name: str, employee_id: str,
                     duration: float, frame=None):
        if not self.configured or not self._notify_capture:
            return
        now = datetime.now()
        caption = _md(
            f"\U0001f7e2 {{BOLD}}Attendance Recorded{{/BOLD}}\n"
            f"\n"
            f"\U0001f464 {{BOLD}}Employee:{{/BOLD}} {employee_name}\n"
            f"\U0001f194 {{BOLD}}ID:{{/BOLD}} {{CODE}}{employee_id}{{/CODE}}\n"
            f"\u23f1 {{BOLD}}Verified:{{/BOLD}} {duration:.1f}s\n"
            f"\U0001f4c5 {{BOLD}}Date:{{/BOLD}} {now.strftime('%Y-%m-%d')}\n"
            f"\u23f0 {{BOLD}}Time:{{/BOLD}} {now.strftime('%H:%M:%S')}"
        )
        image = _encode_frame(frame)
        self._enqueue(TelegramJob("capture", caption, image))

    def send_unknown_alert(self, track_id: int, age: float, frame=None):
        now = time.monotonic()
        with self._lock:
            if now - self._last_unknown < self._cooldown_unknown:
                return
            if not self.configured or not self._notify_unknown:
                return
            self._last_unknown = now
        stamp = datetime.now()
        caption = _md(
            f"\U0001f534 {{BOLD}}Unknown Face Detected{{/BOLD}}\n"
            f"\n"
            f"\u26a0\ufe0f An unrecognised face appeared on camera.\n"
            f"\U0001f50d {{BOLD}}Track:{{/BOLD}} #{track_id}\n"
            f"\u23f1 {{BOLD}}In view:{{/BOLD}} {age:.1f}s\n"
            f"\U0001f4c5 {{BOLD}}Date:{{/BOLD}} {stamp.strftime('%Y-%m-%d')}\n"
            f"\u23f0 {{BOLD}}Time:{{/BOLD}} {stamp.strftime('%H:%M:%S')}\n"
            f"\n"
            f"{{ITALIC}}This person is not enrolled in the system.{{/ITALIC}}"
        )
        image = _encode_frame(frame)
        self._enqueue(TelegramJob("unknown", caption, image))

    def send_test(self) -> str:
        """Synchronous test: returns status string for the UI."""
        if not _HAS_REQUESTS:
            return "The 'requests' library is not installed."
        if not self._token:
            return "Bot token is empty."
        if not self._chat_id:
            return "Chat ID is empty."
        try:
            resp = requests.get(GET_ME.format(token=self._token), timeout=TIMEOUT)
            data = resp.json()
            if not data.get("ok"):
                return f"Bot verification failed: {data.get('description', resp.text)}"
            bot_name = data["result"].get("first_name", "Bot")
            bot_user = data["result"].get("username", "")
        except Exception as exc:
            return f"Connection failed: {exc}"
        try:
            now = datetime.now()
            text = _md(
                f"\u2705 {{BOLD}}Bot Connected Successfully{{/BOLD}}\n"
                f"\n"
                f"\U0001f916 {{BOLD}}Bot:{{/BOLD}} {bot_name} (@{bot_user})\n"
                f"\U0001f4ac {{BOLD}}Chat ID:{{/BOLD}} {{CODE}}{self._chat_id}{{/CODE}}\n"
                f"\U0001f4c5 {{BOLD}}Tested:{{/BOLD}} {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"\n"
                f"{{ITALIC}}Face Attendance notifications will be sent to this chat.{{/ITALIC}}"
            )
            resp = requests.post(
                SEND_MESSAGE.format(token=self._token),
                json={"chat_id": self._chat_id, "text": text,
                      "parse_mode": "MarkdownV2"},
                timeout=TIMEOUT,
            )
            result = resp.json()
            if not result.get("ok"):
                return f"Message failed: {result.get('description', resp.text)}"
            return f"Connected to @{bot_user}. Test message sent successfully."
        except Exception as exc:
            return f"Bot verified, but sending failed: {exc}"

    # -- internal -----------------------------------------------------------

    def _enqueue(self, job: TelegramJob):
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            logging.warning("Telegram queue full, dropping notification")

    def _run(self):
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._send(job)
            except Exception:
                logging.debug("Telegram send failed", exc_info=True)

    def _send(self, job: TelegramJob):
        with self._lock:
            token, chat_id = self._token, self._chat_id
        if not token or not chat_id:
            return
        if job.image:
            files = {"photo": ("capture.jpg", io.BytesIO(job.image), "image/jpeg")}
            data = {"chat_id": chat_id, "caption": job.caption,
                    "parse_mode": "MarkdownV2"}
            requests.post(SEND_PHOTO.format(token=token),
                          data=data, files=files, timeout=TIMEOUT)
        else:
            requests.post(SEND_MESSAGE.format(token=token),
                          json={"chat_id": chat_id, "text": job.caption,
                                "parse_mode": "MarkdownV2"},
                          timeout=TIMEOUT)

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)


# -- helpers ----------------------------------------------------------------

def _encode_frame(frame) -> Optional[bytes]:
    if frame is None:
        return None
    try:
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def _escape(text: str) -> str:
    """Escape all MarkdownV2 special characters."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _md(text: str) -> str:
    """Escape a full message template (everything except our markup tokens).

    Write templates with {BOLD}...{/BOLD}, {ITALIC}...{/ITALIC} and {CODE}...{/CODE}
    placeholders, then pass through this function to get valid MarkdownV2.
    """
    text = _escape(text)
    text = text.replace("\\{BOLD\\}", "*").replace("\\{/BOLD\\}", "*")
    text = text.replace("\\{ITALIC\\}", "_").replace("\\{/ITALIC\\}", "_")
    text = text.replace("\\{CODE\\}", "`").replace("\\{/CODE\\}", "`")
    return text
