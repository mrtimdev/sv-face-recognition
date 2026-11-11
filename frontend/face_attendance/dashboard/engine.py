"""Qt-facing engine: the terminal's workers, driven by a background loop.

No attendance rule is reimplemented here. The same ``CameraManager``,
``RecognitionWorker``, ``FaceTracker``, ``AttendanceService`` and
``PersistenceWorker`` used by ``AttendanceApplication`` are started, and their
output is published through Qt signals instead of an OpenCV window. The loop
runs on its own thread, so widgets never block, and the clean camera frame is
handed to enrollment through ``grab_clean_frame``.
"""
from datetime import datetime
import logging
import os
from pathlib import Path
import threading
import time

import cv2
import numpy as np
from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from ..attendance import AttendanceService
from ..anti_spoof import LIVE_THRESHOLD, SAMPLE_MAX_AGE, live_sample
from ..liveness import MAX_SAMPLE_GAP
from ..camera import CameraManager
from ..capture import face_capture
from ..unknown_capture import save_unknown_capture
from ..enrollment import employee_rows as load_employee_rows
from .. import angle_templates, hrm_directory
from ..models import RecognitionRequest
from ..persistence import PersistenceWorker
from ..power import SleepInhibitor
from ..recognition import RecognitionService, RecognitionWorker
from ..settings import LOCK_PATH, describe_source
from ..tracking import FaceTracker
from ..ui import UIRenderer
from .lock import EngineLock


PUBLISH_INTERVAL = 0.066  # ~15 Hz stats/track updates; frames follow the camera, capped at target_fps.
JOIN_TIMEOUT = 3.0
MAX_RESTART_BACKOFF = 60.0
UNKNOWN_ALERT_EXPIRY = 3600.0


def track_snapshot(track, now, tracker=None):
    """Plain data for Qt widgets; the live FaceTrack stays engine-owned."""
    return {"track_id": track.track_id,
            "employee_id": track.employee_id or "",
            "employee_active": track.employee_active,
            "name": track.employee_name if track.employee_id else "UNKNOWN",
            "last_name": track.employee_last_name if track.employee_id else "",
            "state": track.state.value,
            "progress": round(track.verification_progress, 3),
            "cooldown": round(track.cooldown_remaining, 1),
            "presence": round(track.verified_presence, 1),
            "visible": bool(track.visible),
            "ambiguous": bool(track.ambiguous),
            "identity_valid": bool(track.identity_valid),
            "quality_ok": bool(track.quality_ok),
            "quality_prompt": track.quality_prompt,
            "quality_metrics": dict(track.quality_metrics),
            "spoof_ok": bool(track.spoof_ok),
            "spoof_score": track.spoof_score,
            "spoof_model_scores": track.spoof_model_scores,
            "spoof_progress": track.spoof_progress,
            "spoof_prompt": track.spoof_prompt,
            "liveness_ok": bool(track.liveness_ok),
            "liveness_progress": track.liveness_progress,
            "liveness_prompt": track.liveness_prompt,
            "distance": None if track.recognition_distance is None else round(track.recognition_distance, 3),
            "age": round(now - track.first_seen, 1),
            "error": track.error}


class AttendanceEngine(QObject):
    """Owns the attendance workers; connect to the signals instead of polling."""

    frameReady = pyqtSignal(object)          # annotated BGR frame (numpy)
    _previewAvailable = pyqtSignal()
    tracksReady = pyqtSignal(object)         # list of track snapshots
    statsReady = pyqtSignal(object)          # dict of live statistics
    captureTriggered = pyqtSignal(object)    # CaptureJob when auto-capture fires
    unknownFaceAlert = pyqtSignal(object)   # dict snapshot of unknown face for alerts
    inactiveEmployeeAlert = pyqtSignal(object)  # saved blocked scan observation
    attendanceSaved = pyqtSignal(object)     # SaveResult after a committed row
    attendanceFailed = pyqtSignal(object)    # SaveResult with outcome "error"
    catalogChanged = pyqtSignal(object)      # employee rows after rebuild
    runningChanged = pyqtSignal(bool)
    pausedChanged = pyqtSignal(bool)
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
        self._preview_lock = threading.Lock()
        self._preview_pending = False
        self._preview_values = {}
        self._previewAvailable.connect(self._deliver_preview, Qt.ConnectionType.QueuedConnection)
        self._stop = threading.Event()
        self._sleep_inhibitor = SleepInhibitor()
        self._clean = None
        self._paused = False
        self._thread = None
        self._stats = {}
        self._last_elapsed = 0.0
        self._unknown_alerted = {}
        self._unknown_retry = {}
        self.config = None
        self.catalog = None
        self._build()

    def _build(self):
        """Construct workers for the current settings; may raise for bad input."""
        self._assert_workers_stopped()
        self.config = self.settings.to_config()
        # Match the terminal runtime: avoid OpenCV creating a competing CPU pool
        # for every local recognition/model operation.
        from ..enrollment import FACE_BACKEND_LOCK
        with FACE_BACKEND_LOCK:
            cv2.setNumThreads(self.config.opencv_threads)
        # Taken before loading, so a write racing the load is still seen later.
        self._catalog_seen = self._catalog_signature()
        self._catalog_pending = None
        self.catalog = self._load_catalog()
        self.camera = (CameraManager(self.config) if self._capture_factory is None else
                       CameraManager(self.config, capture_factory=self._capture_factory))
        self.recognition = RecognitionWorker(
            RecognitionService(self.config, self.catalog, backend=self._backend))
        self.persistence = PersistenceWorker(self.config, self.catalog.employee_map, audio_enabled=False)
        self.tracker = FaceTracker(self.config)
        self.attendance = AttendanceService(self.config, self.persistence, async_staging=True)
        # Qt paints the dashboard-wide flash without altering camera pixels.
        self.renderer = UIRenderer(self.config, capture_effects=False)

    def _load_catalog(self):
        return hrm_directory.live_catalog(self.config, allow_empty=True)

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

    def _assert_workers_stopped(self):
        if self.running:
            raise RuntimeError("The previous engine is still stopping. Restart the application if its driver remains blocked.")
        for name in ('camera', 'recognition', 'persistence'):
            worker = getattr(self, name, None)
            if worker is not None and worker.thread.is_alive():
                raise RuntimeError(f"The previous {name} worker has not stopped. Restart the application before starting again.")
        persistence = getattr(self, 'persistence', None)
        staging = getattr(persistence, 'staging_thread', None)
        if staging is not None and staging.is_alive():
            raise RuntimeError('The previous attendance staging worker has not stopped. Restart the application before starting again.')

    def start(self):
        if self.running:
            if self._stop.is_set():
                raise RuntimeError("The previous engine is still stopping. Restart the application if its driver remains blocked.")
            return
        self._assert_workers_stopped()
        self.acquire_lock()
        self._unknown_alerted = {}
        self._unknown_retry = {}
        self._build()
        self._stop.clear()
        self.persistence.start()
        self.recognition.start()
        self.camera.start()
        self._thread = threading.Thread(target=self._run, name="dashboard-engine", daemon=True)
        self._thread.start()
        self._sleep_inhibitor.acquire()
        self.runningChanged.emit(True)
        self.catalogChanged.emit(self.employee_rows())
        self.logMessage.emit(f"Engine started: {describe_source(self.settings.source)} | "
                             f"{self.catalog.enrolled_count} employee(s)")
        self.persistence.log_audit("engine_started",
                                   f"source={describe_source(self.settings.source)}, "
                                   f"enrolled={self.catalog.enrolled_count}")

    def stop(self, on_stage=None):
        """Stop the loop, then close camera, recognition and persistence.

        *on_stage* (optional, called from this thread) hears ``"capture"`` once
        the camera and recognition are closed and ``"saved"`` once queued
        attendance is committed, so a caller can show real shutdown progress.
        """
        report = on_stage or (lambda _stage: None)
        self._stop.set()
        self._sleep_inhibitor.release()
        thread = self._thread
        if thread is not None:
            thread.join(JOIN_TIMEOUT)
            if thread.is_alive():
                logging.error("Engine loop did not stop within %.1fs", JOIN_TIMEOUT)
            else:
                self._thread = None
        # Camera first, then recognition, then persistence: accepted attendance
        # jobs are committed before the database connection closes.
        self.camera.close()
        self.recognition.close()
        report("capture")
        self.persistence.close()
        report("saved")
        with self._clean_lock:
            self._clean = None
        with self._preview_lock:
            self._preview_values.clear()
        self._stats = {}
        if thread is not None:
            self.runningChanged.emit(False)

    def restart(self, settings=None):
        if settings is not None:
            self.settings = settings
        self.stop()
        self.start()

    def shutdown(self, on_stage=None):
        self.stop(on_stage)
        if self._lock is not None:
            self._lock.release()

    def set_paused(self, paused):
        """Pause recording only: the preview and the overlays keep working."""
        paused = bool(paused)
        if paused != self._paused:
            self._paused = paused
            self.pausedChanged.emit(paused)
            self.logMessage.emit("Attendance recording paused" if paused
                                 else "Attendance recording resumed")

    def toggle_paused(self):
        self.set_paused(not self._paused)
        return self._paused

    def grab_clean_frame(self):
        """Copy of the newest frame without overlays, for enrollment samples."""
        with self._clean_lock:
            return None if self._clean is None else self._clean.copy()

    def _queue_preview(self, **values):
        """One pending GUI notification, regardless of UI speed or frame rate."""
        with self._preview_lock:
            self._preview_values.update(values)
            if self._preview_pending:
                return
            self._preview_pending = True
        self._previewAvailable.emit()

    @pyqtSlot()
    def _deliver_preview(self):
        with self._preview_lock:
            values, self._preview_values = self._preview_values, {}
            self._preview_pending = False
        if 'frame' in values:
            self.frameReady.emit(values['frame'])
        if 'tracks' in values:
            self.tracksReady.emit(values['tracks'])
        if 'stats' in values:
            self.statsReady.emit(values['stats'])

    def employee_rows(self):
        return load_employee_rows(self.config.encodings_path, self.config.employees_path)

    def reload_catalog(self):
        """Re-read encodings, mappings and HRM names after any employee change.

        A running engine switches to the new catalog in place: the camera keeps
        running and the next recognition uses the new names and face samples.
        If the files can't be read, it keeps recognizing with the catalog it has.
        """
        if self.running:
            try:
                self._swap_catalog()
            except Exception as exc:
                logging.exception("Could not reload the employee catalog")
                self.errorRaised.emit(f"Could not reload employees: {exc}")
                return
        else:
            self._build()
        self.catalogChanged.emit(self.employee_rows())
        self.logMessage.emit(f"Catalog reloaded: {self.catalog.enrolled_count} employee(s)")

    def _swap_catalog(self):
        # Marked seen first, so an unreadable file is reported once and not on every poll.
        self._catalog_seen = self._catalog_signature()
        self._catalog_pending = None
        catalog = self._load_catalog()
        self.recognition.service.set_catalog(catalog)
        # The persistence worker's repository keeps a reference to this dict, so update it
        # rather than replace it. Stale labels go last: a save in progress always finds one.
        shared = self.persistence.employee_map
        shared.update(catalog.employee_map)
        for label in [label for label in shared if label not in catalog.employee_map]:
            shared.pop(label, None)
        catalog.employee_map = shared
        self.catalog = catalog

    def _catalog_signature(self):
        signature = []
        for path in (self.config.encodings_path, self.config.employees_path,
                     angle_templates.store_path(self.config)):
            try:
                stat = Path(path).stat()
            except OSError:
                signature.append(None)
            else:
                signature.append((stat.st_mtime_ns, stat.st_size))
        signature.append(self._directory_signature())
        return tuple(signature)

    def _directory_signature(self):
        """What the HRM cache says, re-read only when its file changes. A sync that changes
        nobody's name or status must not restart the camera."""
        path = hrm_directory.cache_path(self.config)
        try:
            stat = path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        cached = getattr(self, "_directory_cached", None)
        if cached is None or cached[0] != stamp:
            cached = (stamp, hrm_directory.fingerprint(hrm_directory.load(path)))
            self._directory_cached = cached
        return cached[1]

    def reload_if_catalog_changed(self):
        """Reload when another process (e.g. CLI enrollment) rewrote the catalog.

        Polled from the GUI thread. A change must be seen on two consecutive
        polls, so the dashboard's own writes, which call ``reload_catalog``
        right away, are not reloaded a second time. Returns True on reload.
        """
        current = self._catalog_signature()
        if current == self._catalog_seen:
            self._catalog_pending = None
            return False
        if current != self._catalog_pending:
            self._catalog_pending = current
            return False
        self.logMessage.emit("Employee catalog changed outside the dashboard; reloading")
        self.reload_catalog()
        return True

    def reload_if_directory_changed(self):
        """Apply a finished HRM sync right away. A sync that changed nothing does nothing."""
        if self._catalog_signature() == self._catalog_seen:
            return False
        self.reload_catalog()
        return True

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
        recognition_error, published, last_liveness_log = "", 0.0, float("-inf")
        frame_budget = 1 / max(1.0, float(cfg.target_fps))
        rendered_at, frames_version = float("-inf"), -1
        health_at = float('-inf')
        while not self._stop.is_set():
            started = now = time.monotonic()
            wall_time = time.time()
            frames_version = self.camera.frames.version
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
                if kind == 'capture':
                    self.renderer.animations.capture(saved, now)
                    self.captureTriggered.emit(saved)
                    continue
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
            updates = {}
            # Draw only when something can have changed: a new camera frame, a new
            # recognition result, or (for the scan line and animations) once per budget.
            if new_frame or result is not None or now - rendered_at >= frame_budget:
                rendered_at = now
                updates['frame'] = self.renderer.render_overlay(frame, self.tracker.tracks, now, wall_time,
                                                                status, "" if paused else recognition_error)
            if now - published >= PUBLISH_INTERVAL:
                published = now
                updates['tracks'] = [track_snapshot(track, now, self.tracker)
                                     for track in self.tracker.tracks.values()]
                self._stats = self._statistics(status, preview_fps, recognition_error)
                updates['stats'] = self._stats
                health_path = os.environ.get('SV_FACE_HEALTH_FILE')
                if health_path and now - health_at >= 5:
                    from ..health import write_health
                    try:
                        write_health(health_path, self._stats,
                                     None if packet is None else max(0, now - packet.captured_at),
                                     all(worker.thread.is_alive() for worker in
                                         (self.camera, self.recognition, self.persistence))
                                     and self.persistence.staging_thread.is_alive())
                    except OSError:
                        logging.warning('Could not publish engine health', exc_info=True)
                    health_at = now
                if self._stats["liveness_blocked"] and now - last_liveness_log >= 5.0:
                    last_liveness_log = now
                    self.logMessage.emit("Attendance blocked by liveness: " +
                                         self._stats["liveness_details"].replace("\n", "; "))
                self._expire_unknown_alerts(now)
                self._capture_observations(now, generation, status, paused)
            if updates:
                self._queue_preview(**updates)
            # Keep to target_fps, then wake the moment the camera delivers the next
            # frame instead of sleeping a fixed interval and showing it late.
            delay = frame_budget - (time.monotonic() - started)
            if delay > 0 and self._stop.wait(delay):
                break
            self.camera.frames.wait_newer(frames_version, frame_budget)

    def _capture_observations(self, now, generation, status, paused):
        if paused or status != "CONNECTED":
            return
        for track in self.tracker.tracks.values():
            inactive = track.identity_valid and not track.employee_active
            if inactive:
                if not self.settings.auto_capture_inactive:
                    continue
            elif track.state.value != "UNKNOWN" or not self.settings.auto_capture_unknown:
                continue
            observation = track.unknown_packet
            key = (track.track_id, "inactive" if inactive else "unknown", track.employee_id if inactive else "")
            if (not track.visible or not track.quality_ok or track.ambiguous
                    or key in self._unknown_alerted
                    or now < self._unknown_retry.get(key, 0)
                    or now - track.state_since < (0.2 if inactive else 0.2)
                    or observation is None or observation.generation != generation
                    or not 0 <= now - observation.captured_at <= self.config.max_result_age_sec):
                continue
            try:
                crop, box = face_capture(observation.frame, track.unknown_box,
                                         track.unknown_neighbors)
                info = save_unknown_capture(self.config.capture_dir, crop, box,
                    track.track_id, now - track.first_seen, observation.wall_time,
                    category="inactive_employee" if inactive else "unknown_not_enrolled",
                    employee_id=track.employee_id if inactive else "",
                    employee_name=track.employee_name if inactive else "",
                    liveness_verified=inactive and track.liveness_ok and 0 <= now - track.last_liveness_at <= MAX_SAMPLE_GAP,
                    presentation_verified=inactive and track.spoof_ok and 0 <= now - track.last_spoof_at <= SAMPLE_MAX_AGE)
            except ValueError:
                continue
            except OSError as exc:
                self.errorRaised.emit(f"Face observation capture failed: {exc}")
                self._unknown_retry[key] = now + 5
                continue
            self._unknown_alerted[key] = now
            info["frame"] = crop
            if inactive:
                self.persistence.log_audit("inactive_employee_detected",
                    f"{track.employee_id}: attendance blocked; capture={info['event_id']}")
                self.inactiveEmployeeAlert.emit(info)
            else:
                self.unknownFaceAlert.emit(info)

    def _expire_unknown_alerts(self, now):
        self._unknown_retry = {tid: deadline for tid, deadline in self._unknown_retry.items()
                               if deadline > now}
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
        inactive = [track for track in known if not track.employee_active]
        blocked = [track for track in known if track.employee_active and not live_sample(track.spoof_score)
                   and track.state.value not in ("SUCCESS", "COOLDOWN", "CAPTURING")]
        details = []
        for track in blocked:
            scores = ", ".join(f"{name}: {score:.3f}" for name, score in
                               zip(("V2", "V1SE"), track.spoof_model_scores))
            details.append(f"Track {track.track_id}: {track.spoof_prompt}" +
                           (f" ({scores}; required >= {LIVE_THRESHOLD:.2f})" if scores else ""))
        totals = self.persistence.daily_totals
        today = datetime.now().date().isoformat()
        return {"verified_today": totals.get("count") if totals.get("date") == today else None,
                "quality_blocked": len([t for t in visible if not t.quality_ok or t.ambiguous]),
                "quality_details": "\n".join(f"Track {t.track_id}: " + ("Separate overlapping faces" if t.ambiguous else t.quality_prompt)
                                            for t in visible if not t.quality_ok or t.ambiguous),
                "status": status,
                "paused": self._paused,
                "source": describe_source(self.settings.source),
                "camera_fps": round(self.camera.fps, 1),
                "preview_fps": round(preview_fps, 1),
                "faces": len(visible),
                "known_faces": len(known),
                "inactive_faces": len(inactive),
                "inactive_details": "; ".join(f"{t.employee_name} ({t.employee_id}): attendance blocked" for t in inactive),
                "unknown_faces": len([track for track in visible
                                      if track.quality_ok and not track.ambiguous and track.state.value == "UNKNOWN"]),
                "cooldowns": len([track for track in visible if track.cooldown_remaining > 0]),
                "enrolled": self.catalog.enrolled_count,
                "presence": round(max((track.verified_presence for track in visible), default=0.0), 1),
                "capture_after": cfg.capture_after_sec,
                "latency_ms": round(self._last_elapsed * 1000, 1),
                "liveness_blocked": bool(blocked),
                "liveness_details": "\n".join(details),
                "storage_ready": self.attendance.ready,
                "storage_error": self.attendance.storage_error,
                "recognition_error": recognition_error,
                "queue": self.persistence.jobs.qsize(),
                "staging_queue": self.persistence.staging_jobs.qsize(),
                "queue_max": cfg.persistence_queue_size,
                "db_path": str(cfg.db_path),
                "capture_dir": str(cfg.capture_dir),
                "recognition_error_count": len([track for track in tracks.values() if track.error]),
                "max_detect_faces": getattr(cfg, "max_detect_faces", 5)}
