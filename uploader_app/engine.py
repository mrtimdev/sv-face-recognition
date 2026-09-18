"""Reusable concurrent folder-monitoring upload engine."""
import json
import mimetypes
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests
import websocket
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .storage import CONFIG_FILE, STATE_FILE, fingerprint, read_json, write_json

@dataclass(frozen=True)
class JobEvent:
    path: Path
    relative: str
    status: str
    error: str = ""

class _Handler(FileSystemEventHandler):
    def __init__(self, enqueue): self.enqueue = enqueue
    def on_created(self, event):
        if not event.is_directory: self.enqueue(Path(event.src_path))
    def on_modified(self, event):
        if not event.is_directory: self.enqueue(Path(event.src_path))
    def on_moved(self, event):
        if not event.is_directory: self.enqueue(Path(event.dest_path))

class UploadEngine:
    """Monitor a folder and upload stable files; callbacks may occur on worker threads."""
    def __init__(self, notify: Optional[Callable[[str, object], None]] = None,
                 timeout: int = 120, retries: int = 5):
        self.notify = notify or (lambda _kind, _payload: None)
        self.timeout, self.retries, self.running = timeout, retries, False
        self.root, self.url, self.recursive = Path.cwd(), "", True
        self.observer = self.pool = None
        self.pending, self.lock = set(), threading.Lock()
        self.state, self.state_lock = read_json(STATE_FILE), threading.Lock()
        self.events, self.socket_thread = queue.Queue(), None

    def _socket_url(self):
        base = self.url.split("/api/", 1)[0].rstrip("/")
        scheme = "wss" if base.startswith("https://") else "ws"
        return scheme + base.split("://", 1)[-1] + "/ws/upload-monitor"

    def _send_events(self):
        socket = None
        while self.running or not self.events.empty():
            try:
                event = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if socket is None or not socket.connected:
                    socket = websocket.create_connection(self._socket_url(), timeout=5)
                    socket.recv()
                socket.send(json.dumps(event))
            except Exception:
                socket = None
            finally:
                self.events.task_done()
        if socket is not None:
            socket.close()

    def _publish(self, event):
        if self.running:
            self.events.put({**event, "timestamp": time.time()})

    def _monitor(self, running, message):
        self.notify("monitor", {"running": running, "message": message})
        self._publish({"type": "monitor", "running": running, "message": message})

    def _job(self, path, relative, status, error=""):
        self.notify("job", JobEvent(path, relative, status, error))
        self._publish({"type": "job", "path": str(path), "relativePath": relative,
                       "status": status, "error": error})

    def start(self, folder: str, url: str, recursive=True, batch_size=3):
        self.stop(wait=True)
        self.root, self.url, self.recursive = Path(folder).expanduser().resolve(), url.strip(), recursive
        self.root.mkdir(parents=True, exist_ok=True); self.running = True
        self.pool = ThreadPoolExecutor(max_workers=batch_size, thread_name_prefix="upload")
        self.observer = Observer(); self.observer.schedule(_Handler(self.enqueue), str(self.root), recursive=recursive)
        self.observer.start()
        self.socket_thread = threading.Thread(target=self._send_events, name="upload-monitor-socket", daemon=True)
        self.socket_thread.start(); self._monitor(True, f"Monitoring {self.root}")

    def stop(self, wait=False):
        self.running = False
        if self.observer:
            self.observer.stop()
            if wait: self.observer.join(timeout=5)
            self.observer = None
        if self.pool:
            self.pool.shutdown(wait=wait, cancel_futures=True); self.pool = None
        with self.lock: self.pending.clear()
        self._monitor(False, "Monitoring stopped")

    def scan_existing(self):
        return sum(self.enqueue(p) for p in self.root.glob("**/*" if self.recursive else "*"))

    def enqueue(self, path: Path):
        if not self.running or not self.pool: return False
        try: path = path.resolve(); relative = path.relative_to(self.root)
        except (OSError, ValueError): return False
        if not path.is_file() or path in (CONFIG_FILE, STATE_FILE) or any(p.startswith(".") for p in relative.parts):
            return False
        with self.lock:
            if path in self.pending: return False
            self.pending.add(path)
        relative = str(relative); self._job(path, relative, "Queued")
        self.pool.submit(self._upload, path, relative); return True

    def _stable(self, path):
        previous, stable = -1, 0
        while self.running and path.exists():
            try: size = path.stat().st_size
            except OSError: return False
            stable = stable + 1 if size == previous else 0
            if stable >= 3: return True
            previous = size; time.sleep(1)
        return False

    def _upload(self, path, relative):
        status, error = "Waiting for file", ""; self._job(path, relative, status)
        try:
            if not self._stable(path): status = "Cancelled" if not self.running else "File unavailable"; return
            mark, key = fingerprint(path), str(path)
            with self.state_lock: unchanged = self.state.get(key) == mark
            if unchanged: status = "Unchanged"; return
            for attempt in range(1, self.retries + 1):
                if not self.running: status = "Cancelled"; return
                status, error = f"Uploading ({attempt}/{self.retries})", ""; self._job(path, relative, status)
                try:
                    with path.open("rb") as stream:
                        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                        response = requests.post(self.url, files={"file": (path.name, stream, mime_type)},
                            data={"relative_path": relative}, timeout=self.timeout)
                    if not response.ok:
                        try:
                            message = response.json().get("message")
                        except (ValueError, AttributeError):
                            message = response.text.strip()
                        raise requests.HTTPError(
                            f"{response.status_code}: {message or response.reason}", response=response)
                    with self.state_lock: self.state[key] = mark; write_json(STATE_FILE, self.state)
                    status = "Uploaded"; return
                except (OSError, requests.RequestException) as exc:
                    error = str(exc)
                    if attempt < self.retries:
                        status = f"Retrying ({attempt}/{self.retries})"; self._job(path, relative, status, error)
                        time.sleep(min(2 ** attempt, 30))
            status = "Failed"
        except Exception as exc: status, error = "Failed", str(exc)
        finally:
            with self.lock: self.pending.discard(path)
            self._job(path, relative, status, error)
