"""Qt-facing engine: the terminal's workers, driven by a background loop.

No attendance rule is reimplemented here. The same ``CameraManager``,
``RecognitionWorker``, ``FaceTracker``, ``AttendanceService`` and
``PersistenceWorker`` used by ``AttendanceApplication`` are started, and their
output is published through Qt signals instead of an OpenCV window. The loop
runs on its own thread, so widgets never block, and the clean camera frame is
handed to enrollment through ``grab_clean_frame``.
"""
import logging
import threading
import time

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from ..attendance import AttendanceService
from ..camera import CameraManager
from ..catalog import FaceCatalog
from ..enrollment import employee_rows as load_employee_rows
from ..models import RecognitionRequest
from ..persistence import PersistenceWorker
from ..recognition import RecognitionService, RecognitionWorker
from ..settings import LOCK_PATH, describe_source
from ..tracking import FaceTracker
from ..ui import UIRenderer
from .lock import EngineLock


PUBLISH_INTERVAL = 0.066  # ~15 Hz stats/track updates; frames still follow target_fps.
JOIN_TIMEOUT = 3.0
MAX_RESTART_BACKOFF = 60.0
UNKNOWN_ALERT_EXPIRY = 3600.0


def track_snapshot(track, now, tracker=None):
    """Plain data for Qt widgets; the live FaceTrack stays engine-owned."""
    liveness = tracker.liveness if tracker else None
    blinks = liveness.blink_count(track.track_id) if liveness else 0
    return {"track_id": track.track_id,
            "employee_id": track.employee_id or "",
            "name": track.employee_name if track.employee_id else "UNKNOWN",
            "state": track.state.value,
            "progress": round(track.verification_progress, 3),
            "cooldown": round(track.cooldown_remaining, 1),
            "presence": round(track.verified_presence, 1),
            "visible": bool(track.visible),
            "ambiguous": bool(track.ambiguous),
            "identity_valid": bool(track.identity_valid),
            "liveness_ok": bool(track.liveness_ok),
            "liveness_blinks": blinks,
            "distance": None if track.recognition_distance is None else round(track.recognition_distance, 3),
            "age": round(now - track.first_seen, 1),
            "error": track.error}


class AttendanceEngine(QObject):
    """Owns the attendance workers; connect to the signals instead of polling."""

    frameReady = pyqtSignal(object)          # annotated BGR frame (numpy)
    tracksReady = pyqtSignal(object)         # list of track snapshots
    statsReady = pyqtSignal(object)          # dict of live statistics
    captureTriggered = pyqtSignal(object)    # CaptureJob when auto-capture fires
    unknownFaceAlert = pyqtSignal(object)   # dict snapshot of unknown face for alerts
    attendanceSaved = pyqtSignal(object)     # SaveResult after a committed row
    attendanceFailed = pyqtSignal(object)    # SaveResult with outcome "error"
    catalogChanged = pyqtSignal(object)      # employee rows after rebuild
    runningChanged = pyqtSignal(bool)
    errorRaised = pyqtSignal(str)
    logMessage = pyqtSignal(str)

    def __init__(self, settings, parent=None, backend=None, capture_factory=None,
                 use_lock=True):
        super().__init__(parent)
        self.settings = settings
        self._backend = backend
        self._capture_factory = capture_factory
        self._lock = EngineLock(LOCK_PATH) if use_lock else None
        self._clean_lock = threading.Lock()
        self._stop = threading.Event()
        self._clean = None
        self._paused = False
        self._thread = None
        self._stats = {}
        self._last_elapsed = 0.0
        self._unknown_alerted = {}
        self.config = None
        self.catalog = None
        self._build()

    def _build(self):
        """Construct workers for the current settings; may raise for bad input."""
        self.config = self.settings.to_config()
        self.catalog = FaceCatalog(self.config.encodings_path, self.config.employees_path,
                                    allow_empty=True)
        self.camera = (CameraManager(self.config) if self._capture_factory is None else
                       CameraManager(self.config, capture_factory=self._capture_factory))
        self.recognition = RecognitionWorker(
            RecognitionService(self.config, self.catalog, backend=self._backend))
        self.persistence = PersistenceWorker(self.config, self.catalog.employee_map)
        self.tracker = FaceTracker(self.config)
        self.attendance = AttendanceService(self.config, self.persistence)
        self.renderer = UIRenderer(self.config)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self):
        return self._paused

    @property
    def stats(self):
        return self._stats

    def acquire_lock(self):
        """Refuse a second engine on the same data directory."""
        return None if self._lock is None else self._lock.acquire()

    def start(self):
        if self.running:
            return
        self.acquire_lock()
        self._unknown_alerted = {}
        self._build()
        self._stop.clear()
        self.persistence.start()
        self.recognition.start()
        self.camera.start()
        self._thread = threading.Thread(target=self._run, name="dashboard-engine", daemon=True)
        self._thread.start()
        self.runningChanged.emit(True)
        self.catalogChanged.emit(self.employee_rows())
        self.logMessage.emit(f"Engine started: {describe_source(self.settings.source)} | "
                             f"{self.catalog.enrolled_count} employee(s)")
        self.persistence.log_audit("engine_started",
                                   f"source={describe_source(self.settings.source)}, "
                                   f"enrolled={self.catalog.enrolled_count}")

    def stop(self):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(JOIN_TIMEOUT)
            if thread.is_alive():
                logging.error("Engine loop did not stop within %.1fs", JOIN_TIMEOUT)
        # Camera first, then recognition, then persistence: accepted attendance
        # jobs are committed before the database connection closes.
        self.camera.close()
        self.recognition.close()
        self.persistence.close()
        with self._clean_lock:
            self._clean = None
        self._stats = {}
        if thread is not None:
            self.runningChanged.emit(False)

    def restart(self, settings=None):
        if settings is not None:
            self.settings = settings
        self.stop()
        self.start()

    def shutdown(self):
        self.stop()
        if self._lock is not None:
            self._lock.release()

    def set_paused(self, paused):
        """Pause recording only: the preview and the overlays keep working."""
        paused = bool(paused)
        if paused != self._paused:
            self._paused = paused
            self.logMessage.emit("Attendance recording paused" if paused
                                 else "Attendance recording resumed")

    def toggle_paused(self):
        self.set_paused(not self._paused)
        return self._paused

    def grab_clean_frame(self):
        """Copy of the newest frame without overlays, for enrollment samples."""
        with self._clean_lock:
            return None if self._clean is None else self._clean.copy()

    def employee_rows(self):
        return load_employee_rows(self.config.encodings_path, self.config.employees_path)

    def reload_catalog(self):
        """Re-read encodings and mappings after enrollment or a settings change.

        Workers capture the catalog at construction, so a fresh catalog means
        rebuilding them. A running engine is restarted (camera, recognition and
        persistence all reopen) and the dashboard is told about the new rows.
        """
        if self.running:
            self.restart()
        else:
            self.config = self.settings.to_config()
            self._build()
        self.catalogChanged.emit(self.employee_rows())
        self.logMessage.emit(f"Catalog reloaded: {self.catalog.enrolled_count} employee(s)")

    def _run(self):
        """The coordination loop with auto-restart on failure."""
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._loop()
                break  # Clean exit via _stop
            except Exception as exc:
                logging.exception("Attendance engine loop failed")
                self.errorRaised.emit(str(exc))
                if self._stop.is_set():
                    break
                self.logMessage.emit(f"Engine crashed, restarting in {backoff:.0f}s...")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, MAX_RESTART_BACKOFF)
                if not self._stop.is_set():
                    self.tracker.clear()
                    self.logMessage.emit("Engine loop restarted")

    def _loop(self):
        cfg = self.config
        frame = np.full((cfg.camera_height, cfg.camera_width, 3), (32, 29, 25), np.uint8)
        sequence, submitted, generation = -1, -cfg.detection_interval, None
        preview_fps, count, fps_since = 0.0, 0, time.monotonic()
        recognition_error, published = "", 0.0
        while not self._stop.is_set():
            started = now = time.monotonic()
            wall_time = time.time()
            packet = self.camera.frames.get()
            status = self.camera.status(now)
            paused = self._paused
            new_frame = (packet is not None and packet.sequence != sequence
                         and status == "CONNECTED")
            if new_frame:
                if packet.generation != generation:
                    submitted, generation = -cfg.detection_interval, packet.generation
                sequence, frame = packet.sequence, packet.frame
                with self._clean_lock:
                    self._clean = packet.frame
                self.tracker.advance(packet)
                count += 1
            if status != "CONNECTED":
                self.tracker.suspend()
            result = self.recognition.results.take()
            if result is not None:
                self._last_elapsed = result.elapsed
            if result is not None and status == "CONNECTED":
                if self.tracker.apply(result, now):
                    recognition_error = ""
                elif result.error:
                    recognition_error = result.error
            if new_frame and sequence - submitted >= cfg.detection_interval:
                self.recognition.requests.put(RecognitionRequest(packet, self.tracker.hints()))
                submitted = sequence
            for kind, saved in self.attendance.poll(self.tracker.tracks, now, wall_time):
                self.renderer.animations.result(kind, saved, now)
                if kind == "success":
                    self.attendanceSaved.emit(saved)
                else:
                    self.attendanceFailed.emit(saved)
            if not paused:
                for job in self.attendance.update(self.tracker.tracks, packet, now,
                                                  status == "CONNECTED"):
                    self.renderer.animations.capture(job, now)
                    self.captureTriggered.emit(job)
                self.attendance.observe(self.tracker.tracks, now, wall_time)
            elapsed = now - fps_since
            if elapsed >= 1.0:
                preview_fps, count, fps_since = count / elapsed, 0, now
            image = self.renderer.render_overlay(frame, self.tracker.tracks, now, wall_time,
                                                 status, "" if paused else recognition_error)
            self.frameReady.emit(image)
            if now - published >= PUBLISH_INTERVAL:
                published = now
                self.tracksReady.emit([track_snapshot(track, now, self.tracker)
                                       for track in self.tracker.tracks.values()])
                self._stats = self._statistics(status, preview_fps, recognition_error)
                self.statsReady.emit(self._stats)
                self._expire_unknown_alerts(now)
                for track in self.tracker.tracks.values():
                    if (track.visible and not track.employee_id
                            and track.track_id not in self._unknown_alerted
                            and now - track.first_seen >= 2.0):
                        self._unknown_alerted[track.track_id] = now
                        with self._clean_lock:
                            snap = None if self._clean is None else self._clean.copy()
                        self.unknownFaceAlert.emit({
                            "track_id": track.track_id,
                            "age": round(now - track.first_seen, 1),
                            "frame": snap,
                        })
            delay = 1 / cfg.target_fps - (time.monotonic() - started)
            if delay > 0:
                self._stop.wait(delay)

    def _expire_unknown_alerts(self, now):
        expired = [tid for tid, ts in self._unknown_alerted.items()
                   if now - ts > UNKNOWN_ALERT_EXPIRY]
        for tid in expired:
            del self._unknown_alerted[tid]

    def _statistics(self, status, preview_fps, recognition_error):
        """Snapshot of the engine state for the Live Monitor cards."""
        cfg = self.config
        tracks = self.tracker.tracks
        visible = [track for track in tracks.values() if track.visible]
        known = [track for track in visible if track.employee_id and track.identity_valid]
        return {"status": status,
                "paused": self._paused,
                "source": describe_source(self.settings.source),
                "camera_fps": round(self.camera.fps, 1),
                "preview_fps": round(preview_fps, 1),
                "faces": len(visible),
                "known_faces": len(known),
                "unknown_faces": len([track for track in visible
                                      if not (track.employee_id and track.identity_valid)]),
                "cooldowns": len([track for track in visible if track.cooldown_remaining > 0]),
                "enrolled": self.catalog.enrolled_count,
                "presence": round(max((track.verified_presence for track in visible), default=0.0), 1),
                "capture_after": cfg.capture_after_sec,
                "latency_ms": round(self._last_elapsed * 1000, 1),
                "storage_ready": self.attendance.ready,
                "storage_error": self.attendance.storage_error,
                "recognition_error": recognition_error,
                "queue": self.persistence.jobs.qsize(),
                "queue_max": cfg.persistence_queue_size,
                "db_path": str(cfg.db_path),
                "capture_dir": str(cfg.capture_dir),
                "recognition_error_count": len([track for track in tracks.values() if track.error]),
                "max_detect_faces": getattr(cfg, "max_detect_faces", 5)}