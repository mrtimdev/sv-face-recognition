"""Bounded attendance jobs, CSV observations and cached nonblocking audio."""
import csv
import logging
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

from .channels import LatestValue
from .audio import SoundPlayer
from .repository import AttendanceRepository
from .models import SaveResult
from .storage import SnapshotService

OBSERVATION_LOG_BYTES = 10 * 1024 * 1024
OBSERVATION_LOG_BACKUPS = 3


class PersistenceWorker:
    def __init__(self, config, employee_map, repository_factory=AttendanceRepository, snapshots=None, audio_enabled=True):
        self.config, self.employee_map = config, employee_map
        self.repository_factory = repository_factory
        self.snapshots = snapshots or SnapshotService(config.capture_dir)
        self.jobs = queue.Queue(maxsize=config.persistence_queue_size)
        self.observations = queue.Queue(maxsize=32)
        self._audit_queue = queue.Queue(maxsize=64)
        self.results = queue.Queue(maxsize=config.persistence_queue_size + 1)
        self.startup = LatestValue()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, name="persistence", daemon=True)
        self.sound = None
        self.audio_enabled = audio_enabled
        self.daily_totals = {}

    def start(self):
        self.thread.start()

    def submit(self, job):
        if self.stop_event.is_set():
            return False
        try:
            self.jobs.put_nowait(job)
            self.wake.set()
            return True
        except queue.Full:
            return False

    def observe(self, event):
        try:
            self.observations.put_nowait(event)
            self.wake.set()
        except queue.Full:
            pass  # Optional observation telemetry never displaces attendance.

    def log_audit(self, action, detail="", actor="system"):
        try:
            self._audit_queue.put_nowait((action, detail, actor))
            self.wake.set()
        except queue.Full:
            pass

    def _init_audio(self):
        if self.audio_enabled:
            self.sound = SoundPlayer(self.config.alert_path, self.config.alert_cooldown_sec)

    def _alert(self):
        if self.sound is not None:
            self.sound.play()

    def _observe(self, event):
        try:
            path = self.config.log_path
            if path.exists() and path.stat().st_size >= OBSERVATION_LOG_BYTES:
                for index in range(OBSERVATION_LOG_BACKUPS, 0, -1):
                    source = path if index == 1 else Path(f"{path}.{index - 1}")
                    if source.exists():
                        source.replace(Path(f"{path}.{index}"))
            exists = path.exists() and path.stat().st_size > 0
            # Observations contain names; create private files from the outset.
            descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if not exists:
                    writer.writerow(["timestamp", "name", "status"])
                writer.writerow([datetime.fromtimestamp(event.timestamp).isoformat(timespec="seconds"),
                                 event.name, "UNKNOWN" if event.name == "UNKNOWN" else "KNOWN"])
        except OSError:
            logging.exception("Optional CSV observation log failed")
        if event.alert:
            try:
                self._alert()
            except Exception:
                logging.debug("Optional observation sound failed", exc_info=True)

    def _refresh_totals(self, repository):
        self.daily_totals = repository.verified_today()

    @staticmethod
    def _close_repository(repository):
        if repository is not None:
            try:
                repository.close()
            except Exception:
                logging.warning("Storage connection cleanup failed", exc_info=True)

    def _connect_repository(self):
        while not self.stop_event.is_set():
            repository = None
            try:
                repository = self.repository_factory(self.config, self.employee_map,
                                                     self.config.cooldown_sec)
                self._refresh_totals(repository)
                self.startup.put((repository.load_cooldowns(), ""))
                return repository
            except Exception as exc:
                self._close_repository(repository)
                self.daily_totals = {}
                self.startup.put((None, str(exc)))
                self.stop_event.wait(self.config.retry_sec)
        return None

    def _run(self):
        repository = None
        try:
            try:
                self._init_audio()
            except Exception:
                # Optional sound must never disable attendance persistence.
                logging.warning("Audio initialization failed; continuing silently", exc_info=True)
            repository = self._connect_repository()
            last_checkpoint = last_totals = time.monotonic()
            while not self.stop_event.is_set() or not self.jobs.empty():
                if repository is None:
                    repository = self._connect_repository()
                    if repository is None:
                        # Shutdown during an outage: report every accepted job
                        # as failed instead of silently abandoning pending IDs.
                        while not self.jobs.empty():
                            job = self.jobs.get_nowait()
                            try:
                                self.results.put_nowait(SaveResult(job, "error", error="Storage unavailable during shutdown"))
                            except queue.Full:
                                logging.error("Cannot deliver shutdown failure for %s", job.event_id)
                            self.jobs.task_done()
                        break
                    last_checkpoint = last_totals = time.monotonic()
                self.wake.clear()
                try:
                    now = time.monotonic()
                    if now - last_totals >= 5.:
                        self._refresh_totals(repository)
                        last_totals = now
                    if now - last_checkpoint >= 3600:
                        repository.checkpoint()
                        last_checkpoint = now
                    try:
                        job = self.jobs.get_nowait()
                    except queue.Empty:
                        job = None
                    if job is not None:
                        try:
                            result = repository.record(job, self.snapshots)
                        except Exception as exc:
                            result = SaveResult(job, "error", error=str(exc))
                        while True:
                            try:
                                self.results.put(result, timeout=0.1)
                                break
                            except queue.Full:
                                if self.stop_event.is_set():
                                    break
                        self.jobs.task_done()
                        if result.outcome == "saved":
                            try:
                                self._alert()
                            except Exception:
                                logging.debug("Optional attendance sound failed", exc_info=True)
                            repository.audit("attendance_saved",
                                             f"{job.employee_name} ({job.employee_id}), {job.duration:.1f}s")
                            self._refresh_totals(repository)
                        elif result.outcome == "error":
                            raise RuntimeError(result.error)
                        continue
                    try:
                        self._observe(self.observations.get_nowait())
                    except queue.Empty:
                        pass
                    try:
                        action, detail, actor = self._audit_queue.get_nowait()
                        repository.audit(action, detail, actor)
                    except queue.Empty:
                        self.wake.wait(0.1)
                except Exception as exc:
                    logging.warning("Storage interrupted; reconnecting: %s", exc)
                    self.daily_totals = {}
                    self.startup.put((None, str(exc)))
                    self._close_repository(repository)
                    repository = None
                    self.stop_event.wait(self.config.retry_sec)
        finally:
            self._close_repository(repository)
            if self.sound is not None:
                try:
                    self.sound.stop()
                except Exception:
                    logging.debug("Audio cleanup failed", exc_info=True)

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                logging.error("Persistence worker did not finish within 10s; "
                              "%d job(s) may be lost", self.jobs.qsize())
