"""Camera ownership and reconnects live exclusively on the capture thread."""
import logging
import threading
import time

import cv2

from .channels import LatestValue
from .models import FramePacket


MAX_RECONNECT_BACKOFF = 30.0


MIN_CAMERA_FPS = 30.0


def fit_frame(frame, max_width, max_height):
    """Shrink *frame* to fit inside max_width x max_height, keeping its shape.

    Cameras often ignore the requested size (a Mac webcam asked for 1250x780
    sends 1280x720). Resizing straight to the requested size would stretch
    every face, which makes recognition less certain and slower to confirm.
    """
    height, width = frame.shape[:2]
    if width <= max_width and height <= max_height:
        return frame
    scale = min(max_width / width, max_height / height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA)


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
            # Low latency FFmpeg options for RTSP/network camera streams
            import os
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|max_delay;50000"
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
            # Ask for at least 30 fps: a webcam told 15 fps lengthens its exposure,
            # which blurs moving faces and delivers each frame later.
            cap.set(cv2.CAP_PROP_FPS, max(float(cfg.target_fps), MIN_CAMERA_FPS))
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
                    cfg_w, cfg_h = getattr(self.config, "camera_width", 1280), getattr(self.config, "camera_height", 720)
                    while not self.stop_event.is_set():
                        ok, frame = cap.read()
                        now = self._clock()
                        if not ok or frame is None or frame.size == 0:
                            self._set_status("RECONNECTING")
                            break
                        frame = fit_frame(frame, cfg_w, cfg_h)
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
