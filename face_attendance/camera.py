"""Camera ownership and reconnects live exclusively on the capture thread."""
import logging
import threading
import time

import cv2

from .channels import LatestValue
from .models import FramePacket


MAX_RECONNECT_BACKOFF = 30.0


class CameraManager:
    def __init__(self, config, capture_factory=None, clock=time.monotonic):
        self.config = config
        self.frames = LatestValue()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._factory = capture_factory or self._open
        self._clock = clock
        self._lock = threading.Lock()
        self._status = "CONNECTING"
        self.fps = 0.0

    def _open(self):
        cfg = self.config
        if isinstance(cfg.source, str) and "://" in cfg.source:
            # Timeouts must be supplied at open, and are supported by FFmpeg.
            cap = cv2.VideoCapture(cfg.source, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, cfg.camera_timeout_ms,
                cv2.CAP_PROP_READ_TIMEOUT_MSEC, cfg.camera_timeout_ms,
            ])
        else:
            cap = cv2.VideoCapture(cfg.source)
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.camera_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.camera_height)
            cap.set(cv2.CAP_PROP_FPS, cfg.target_fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # best effort; backend dependent
        return cap

    def _set_status(self, value):
        with self._lock:
            self._status = value

    def status(self, now):
        with self._lock:
            status = self._status
        packet = self.frames.get()
        if status == "CONNECTED" and (packet is None or now - packet.captured_at > self.config.camera_stale_sec):
            return "RECONNECTING"
        return status

    def start(self):
        self.thread.start()

    def _run(self):
        sequence, generation = 0, 0
        retry_delay = min(self.config.reconnect_sec, MAX_RECONNECT_BACKOFF)
        backoff = retry_delay
        while not self.stop_event.is_set():
            cap = None
            try:
                self._set_status("CONNECTING" if generation == 0 else "RECONNECTING")
                cap = self._factory()
                if not cap.isOpened():
                    self._set_status("CAMERA DISCONNECTED")
                else:
                    generation += 1
                    count, fps_since = 0, self._clock()
                    while not self.stop_event.is_set():
                        ok, frame = cap.read()
                        now = self._clock()
                        if not ok or frame is None or frame.size == 0:
                            self._set_status("RECONNECTING")
                            break
                        # Opening succeeds even when a stream never yields a frame.
                        # Reset retry pressure only after actual capture succeeds.
                        backoff = retry_delay
                        sequence += 1
                        count += 1
                        if now - fps_since >= 1:
                            self.fps = count / (now - fps_since)
                            count, fps_since = 0, now
                        self.frames.put(FramePacket(sequence, now, time.time(), generation, frame))
                        self._set_status("CONNECTED")
            except Exception:
                logging.exception("Camera connection/read failed")
                self._set_status("CAMERA DISCONNECTED")
            finally:
                self.fps = 0.0
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        # Driver cleanup errors must not kill the reconnect loop.
                        logging.exception("Camera release failed")
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, MAX_RECONNECT_BACKOFF)
        self._set_status("STOPPED")

    def close(self):
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(self.config.camera_timeout_ms / 1000 + 1.0)
        if self.thread.is_alive():
            # Some USB drivers do not implement read timeouts. Do not race release/read.
            logging.error("Camera driver did not return from read; its daemon thread will exit with the process")
