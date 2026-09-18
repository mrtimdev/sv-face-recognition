"""Bounded attendance jobs, CSV observations and cached nonblocking audio."""
import csv
import logging
import queue
import threading
import time
from datetime import datetime

from .channels import LatestValue
from .repository import AttendanceRepository
from .storage import SnapshotService


class PersistenceWorker:
    def __init__(self, config, employee_map, repository_factory=AttendanceRepository, snapshots=None):
        self.config, self.employee_map = config, employee_map
        self.repository_factory = repository_factory
        self.snapshots = snapshots or SnapshotService(config.capture_dir)
        self.jobs = queue.Queue(maxsize=config.persistence_queue_size)
        self.observations = queue.Queue(maxsize=32)
        self.results = queue.Queue(maxsize=config.persistence_queue_size + 1)
        self.startup = LatestValue()
        self.stop_event, self.wake = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, name="persistence", daemon=True)
        self.wave, self.playback = None, None
        self.last_alert = float("-inf")

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

    def _init_audio(self):
        if not self.config.alert_path.exists():
            return
        try:
            import simpleaudio
            self.wave = simpleaudio.WaveObject.from_wave_file(str(self.config.alert_path))
        except Exception as exc:
            logging.info("Optional audio unavailable: %s", exc)

    def _alert(self):
        now = time.monotonic()
        if self.wave is not None and now - self.last_alert >= self.config.alert_cooldown_sec:
            try:
                self.playback = self.wave.play()
                self.last_alert = now
            except Exception:
                logging.exception("Optional audio playback failed")

    def _observe(self, event):
        try:
            path = self.config.log_path
            exists = path.exists() and path.stat().st_size > 0
            with path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                if not exists:
                    writer.writerow(["timestamp", "name", "status"])
                writer.writerow([datetime.fromtimestamp(event.timestamp).isoformat(timespec="seconds"),
                                 event.name, "UNKNOWN" if event.name == "UNKNOWN" else "KNOWN"])
        except OSError:
            logging.exception("Optional CSV observation log failed")
        if event.alert:
            self._alert()

    def _run(self):
        repository = None
        self._init_audio()
        try:
            while not self.stop_event.is_set():
                try:
                    repository = self.repository_factory(self.config.db_path, self.employee_map,
                                                         self.config.cooldown_sec)
                    self.startup.put((repository.load_cooldowns(), ""))
                    break
                except Exception as exc:
                    if repository is not None:
                        repository.close()
                        repository = None
                    self.startup.put((None, str(exc)))
                    self.stop_event.wait(self.config.retry_sec)
            if repository is None:
                return
            while not self.stop_event.is_set() or not self.jobs.empty():
                self.wake.clear()
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    job = None
                if job is not None:
                    result = repository.record(job, self.snapshots)
                    while True:
                        try:
                            self.results.put(result, timeout=0.1)
                            break
                        except queue.Full:
                            if self.stop_event.is_set():
                                break
                    if result.outcome == "saved":
                        self._alert()
                        logging.info("Attendance saved for employee %s: %s", job.employee_id, result.snapshot)
                    elif result.outcome == "error":
                        logging.error("Attendance save failed: %s", result.error)
                    self.jobs.task_done()
                    continue
                try:
                    self._observe(self.observations.get_nowait())
                except queue.Empty:
                    self.wake.wait(0.1)
        finally:
            if repository is not None:
                repository.close()
            if self.playback is not None:
                self.playback.stop()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread.ident is not None:
            # Finish the small, bounded set of accepted attendance jobs before exiting.
            self.thread.join()
