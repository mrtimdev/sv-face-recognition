from dataclasses import replace
import threading
import time
import queue

import numpy as np

from face_attendance.attendance import AttendanceService
from face_attendance.config import Config
from face_attendance.models import CaptureJob, FramePacket, SaveResult
from face_attendance.persistence import PersistenceWorker
from tests.test_attendance import verified


def test_slow_spool_does_not_block_monitoring_or_report_early_success(tmp_path):
    config = replace(Config(), db_path=tmp_path / 'attendance.db', capture_dir=tmp_path / 'captures',
                     employees_path=tmp_path / 'employees.json', log_path=tmp_path / 'events.csv')
    worker = PersistenceWorker(config, {}, audio_enabled=False)
    submit = worker.submit
    entered, release = threading.Event(), threading.Event()

    def slow_submit(job):
        entered.set()
        assert release.wait(5)
        return submit(job)

    worker.submit = slow_submit
    worker.start()
    service = AttendanceService(config, worker, async_staging=True)
    tracks = {1: verified()}
    packet = FramePacket(100, 10, 1000, 1, np.full((240, 320, 3), 57, np.uint8))
    try:
        assert worker.startup.ready.wait(2)
        service.poll(tracks, 10, 1000)
        started = time.monotonic()
        assert service.update(tracks, packet, 10, True) == []
        assert time.monotonic() - started < .25
        assert entered.wait(1)
        assert service.poll(tracks, 10.1, 1000.1) == []
        assert service.last_attendance is None
        # Queue statistics must also be usable while disk staging is stalled.
        assert worker.jobs.qsize() == 0
        release.set()
        events = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            events.extend(service.poll(tracks, 10.2, 1000.2))
            if any(kind == 'success' for kind, _ in events):
                break
            time.sleep(.005)
        kinds = [kind for kind, _ in events]
        assert kinds.index('capture') < kinds.index('success')
    finally:
        release.set()
        worker.close()
    assert not worker.staging_thread.is_alive()
    assert not worker.thread.is_alive()


def test_staging_failure_does_not_confirm_capture(tmp_path):
    config = replace(Config(), db_path=tmp_path / 'attendance.db')
    worker = PersistenceWorker(config, {}, audio_enabled=False)
    worker.submit = lambda job: False
    worker.start()
    service = AttendanceService(config, worker, async_staging=True)
    tracks = {1: verified()}
    packet = tracks[1].evidence_packet
    try:
        assert worker.startup.ready.wait(2)
        service.poll(tracks, 10, 1000)
        assert service.update(tracks, packet, 10, True) == []
        deadline = time.monotonic() + 2
        events = []
        while not events and time.monotonic() < deadline:
            events = service.poll(tracks, 10.1, 1000.1)
            time.sleep(.005)
        assert [kind for kind, _ in events] == ['error']
        assert not service.pending
        assert service.last_attendance is None
    finally:
        worker.close()


def test_preview_queue_counter_never_waits_for_a_spool_write_lock(tmp_path):
    from face_attendance.durable_jobs import DurableJobs
    jobs = DurableJobs(tmp_path / 'pending.sqlite', 8)
    entered, release = threading.Event(), threading.Event()

    def writing():
        with jobs._lock:
            entered.set()
            release.wait(3)

    writer = threading.Thread(target=writing)
    writer.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        assert jobs.qsize() == 0
        assert time.monotonic() - started < .1
    finally:
        release.set()
        writer.join(1)


def test_fast_database_result_waits_for_durable_capture_confirmation():
    from tests.test_attendance import FakePersistence
    worker = FakePersistence()
    worker.stage = worker.submit
    worker.staging_results = queue.Queue()
    service = AttendanceService(Config(), worker, async_staging=True)
    service.ready = True
    tracks = {1: verified()}
    service.update(tracks, tracks[1].evidence_packet, 10, True)
    job = worker.jobs[0]
    worker.results.put(SaveResult(job, 'saved', 1000))
    assert service.poll(tracks, 10.1, 1000.1) == []
    worker.staging_results.put((job, True))
    assert [kind for kind, _ in service.poll(tracks, 10.2, 1000.2)] == ['capture', 'success']


def test_live_frames_continue_while_capture_disk_staging_is_blocked(tmp_path):
    from face_attendance.dashboard.engine import AttendanceEngine
    from tests.test_dashboard import _app, temp_settings, FakeBackend, EngineTests
    app = _app()
    engine = AttendanceEngine(temp_settings(tmp_path), backend=FakeBackend(),
                              capture_factory=lambda: EngineTests.Capture(), use_lock=False)
    frames = []
    engine.frameReady.connect(lambda frame: frames.append(time.monotonic()))
    entered, release = threading.Event(), threading.Event()
    try:
        engine.start()
        deadline = time.monotonic() + 3
        while len(frames) < 2 and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert len(frames) >= 2
        submit = engine.persistence.submit
        def slow_submit(job):
            entered.set()
            assert release.wait(5)
            return submit(job)
        engine.persistence.submit = slow_submit
        job = CaptureJob('slow-disk', 99, 'E99', 'Test', time.time(), 3,
                         np.zeros((64, 64, 3), np.uint8))
        assert engine.persistence.stage(job)
        assert entered.wait(1)
        before = len(frames)
        deadline = time.monotonic() + .4
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert len(frames) >= before + 3
    finally:
        release.set()
        engine.shutdown()
        app.processEvents()
    assert not engine.persistence.staging_thread.is_alive()
