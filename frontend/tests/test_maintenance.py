from dataclasses import replace
from pathlib import Path
import sqlite3
import numpy as np
import pytest

from face_attendance.config import Config
from face_attendance.models import CaptureJob
from face_attendance.repository import AttendanceRepository
from face_attendance.storage import SnapshotService, evidence_files
from face_attendance.maintenance import backup, verify_backup, reassign_event, purge_evidence


def test_retention_preserves_pending_and_rejected_evidence_and_history(tmp_path):
    config = Config(db_path=tmp_path / 'attendance.db', hrm_destination='https://a.test')
    repo = AttendanceRepository(config, cooldown_sec=0)
    snapshots = SnapshotService(tmp_path / 'captures')
    job = CaptureJob('delivered', 1, 'E1', 'Employee', 1000, 3, np.zeros((64,64,3), np.uint8))
    saved = repo.record(job, snapshots)
    repo.record(replace(job, event_id='pending'), snapshots)
    rejected = repo.record(replace(job, event_id='rejected'), snapshots)
    repo.resolve_delivery('delivered', 'saved')
    repo.resolve_delivery('rejected', 'rejected')
    assert [row['event_id'] for row in purge_evidence(repo, snapshots.directory, 2000)] == ['delivered']
    assert Path(saved.snapshot).exists()
    purge_evidence(repo, snapshots.directory, 2000, apply=True)
    assert not any(p.exists() for p in evidence_files(saved.snapshot))
    assert Path(rejected.snapshot).exists()
    assert repo.connection.execute('SELECT count(*) FROM attendance').fetchone()[0] == 3
    with pytest.raises(ValueError):
        reassign_event(repo, 'pending', 'https://wrong.test', 'https://b.test')
    reassign_event(repo, 'pending', 'https://a.test', 'https://b.test')
    assert repo.pending_outbox(destination='https://b.test')[0]['eventId'] == 'pending'
    repo.close()


def test_backup_can_be_restored_and_tampering_is_detected(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    config = Config(db_path=data / 'attendance.db', capture_dir=data / 'captures',
                    employees_path=data / 'employees.json', encodings_path=data / 'templates.pickle')
    repo = AttendanceRepository(config)
    repo.record(CaptureJob('event', 1, 'E1', 'Employee', 1000, 3, np.zeros((64,64,3), np.uint8)),
                SnapshotService(config.capture_dir))
    repo.close()
    destination = tmp_path / 'backup'
    backup(config, destination)
    assert verify_backup(destination)
    # Restore the database through SQLite into an isolated destination.
    with sqlite3.connect(destination / 'attendance.db') as source, sqlite3.connect(tmp_path / 'restored.db') as restored:
        source.backup(restored)
        assert restored.execute('SELECT event_id FROM attendance').fetchone()[0] == 'event'
        assert restored.execute('SELECT count(*) FROM attendance_outbox').fetchone()[0] == 1
    (destination / 'attendance.db').write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        verify_backup(destination)
