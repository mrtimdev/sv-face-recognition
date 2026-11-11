from dataclasses import replace
import queue
import numpy as np
import pytest

from face_attendance.durable_jobs import DurableJobs
from face_attendance.models import CaptureJob


def test_restart_replays_exact_evidence_id_and_destination(tmp_path):
    path = tmp_path / 'pending.sqlite'
    frame = np.arange(1200, dtype=np.uint8).reshape(20, 20, 3)
    job = CaptureJob('id', 1, 'E1', 'Employee', 1000, 3, frame,
                     context_frame=frame, destination='https://original.test')
    queue1 = DurableJobs(path, 1)
    queue1.put_nowait(job)
    queue1.get_nowait()  # process dies before acknowledging persistence
    queue2 = DurableJobs(path, 1)
    recovered = queue2.get_nowait()
    assert recovered.event_id == job.event_id
    assert recovered.destination == job.destination
    assert np.array_equal(recovered.frame, frame)
    assert np.array_equal(recovered.context_frame, frame)
    queue2.retry()
    assert queue2.get_nowait().event_id == 'id'
    queue2.task_done()
    assert DurableJobs(path, 1).empty()


def test_capacity_does_not_drop_already_staged_events(tmp_path):
    jobs = DurableJobs(tmp_path / 'pending.sqlite', 1)
    job = CaptureJob('id', 1, 'E1', 'Employee', 1000, 3, np.zeros((20, 20, 3), np.uint8))
    jobs.put_nowait(job)
    with pytest.raises(queue.Full):
        jobs.put_nowait(replace(job, event_id='other'))
    assert jobs.qsize() == 1
    assert jobs.get_nowait().event_id == 'id'


def test_replay_does_not_collapse_distinct_visits_into_one_cooldown(tmp_path):
    from face_attendance.repository import AttendanceRepository
    from face_attendance.storage import SnapshotService
    jobs = DurableJobs(tmp_path / 'pending.sqlite', 3)
    job = CaptureJob('id', 1, 'E1', 'Employee', 1000, 3, np.zeros((20, 20, 3), np.uint8))
    jobs.put_nowait(job)
    jobs.put_nowait(replace(job, event_id='later', captured_at=1060))
    jobs.put_nowait(replace(job, event_id='duplicate', captured_at=1061))
    repo = AttendanceRepository(tmp_path / 'attendance.db', clock=lambda: 5000)
    snapshots = SnapshotService(tmp_path / 'captures')
    for expected in ('saved', 'saved', 'duplicate'):
        assert repo.record(jobs.get_nowait(), snapshots).outcome == expected
        jobs.task_done()
    repo.close()
