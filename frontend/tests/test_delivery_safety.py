from dataclasses import replace
import sqlite3

import numpy as np

from face_attendance.config import Config
from face_attendance.destination import destination_id
from face_attendance.models import CaptureJob
from face_attendance.repository import AttendanceRepository
from face_attendance.report import AttendanceReader
from face_attendance.storage import SnapshotService
from tests.test_live_attendance_angles import UplinkTests, wait_for


def test_destination_and_rejection_survive_restart(tmp_path):
    config = Config(db_path=tmp_path / 'attendance.db', hrm_destination='https://a.test')
    repo = AttendanceRepository(config)
    job = CaptureJob('event', 1, 'E1', 'Employee', 1000, 3, np.zeros((64, 64, 3), np.uint8))
    assert repo.record(job, SnapshotService(tmp_path / 'captures')).outcome == 'saved'
    assert not repo.pending_outbox(destination='https://b.test')
    assert not repo.pending_outbox(destination='')
    repo.resolve_delivery('event', 'saved', destination='https://b.test')
    assert len(repo.pending_outbox(destination='https://a.test')) == 1
    repo.resolve_delivery('event', 'rejected', destination='https://a.test')
    repo.close()
    repo = AttendanceRepository(config)
    assert not repo.pending_outbox()
    reader = AttendanceReader(config)
    assert reader.outbox_counts()['rejected'] == 1
    assert reader.outbox_counts()['synced'] == 0
    assert reader.delivery_issues()[0]['delivery_reason'] == 'HRM rejected this event'
    reader.close()
    repo.close()


def test_legacy_pending_is_not_assigned_to_current_server(tmp_path):
    config = Config(db_path=tmp_path / 'attendance.db', hrm_destination='https://new.test')
    with sqlite3.connect(config.db_path) as db:
        db.execute('CREATE TABLE attendance_outbox(event_id TEXT PRIMARY KEY, payload TEXT, created_at TEXT, synced_at TEXT)')
        db.execute("INSERT INTO attendance_outbox VALUES ('old', '{}', '2026', NULL)")
        db.execute("INSERT INTO attendance_outbox VALUES ('resolved', '{}', '2026', '2026')")
    repo = AttendanceRepository(config)
    assert not repo.pending_outbox(destination=config.hrm_destination)
    reader = AttendanceReader(config)
    assert reader.outbox_counts()['unassigned'] == 1
    assert reader.outbox_counts()['legacy_resolved'] == 1
    reader.close()
    repo.close()


def test_url_canonicalization_preserves_server_boundaries():
    assert destination_id('https://HRM.test:443/') == 'https://hrm.test'
    assert destination_id('https://hrm.test/company') != destination_id('https://hrm.test/other')


class DestinationSwitchTests(UplinkTests):
    def test_periodic_sync_picks_up_an_event_when_the_save_wakeup_was_missed(self):
        self._uplink(tick_ms=20)
        self._check_in('timer-event')
        assert wait_for(lambda: self.link.sent)
        assert self.link.sent[0]['eventId'] == 'timer-event'

    def test_ack_timeout_retries_without_waiting_for_the_periodic_tick(self):
        self._check_in('timeout-event')
        uplink = self._uplink(ack_timeout_ms=20)
        uplink.kick()
        assert wait_for(lambda: len(self.link.sent) >= 2)
        assert self.link.sent[0]['eventId'] == self.link.sent[1]['eventId']

    def test_closed_uplink_does_not_send_a_queued_batch(self):
        uplink = self._uplink()
        batch = (uplink._generation, self.link.destination, self.config, [('event', '{}')])
        uplink.close()
        uplink._on_batch(batch)
        uplink.kick()
        assert not self.link.sent

    def test_old_batch_is_discarded_after_server_switch(self):
        uplink = self._uplink()
        generation = uplink._generation
        self.link.connectedChanged.emit(False)
        self.link.destination = 'https://other.test'
        uplink._on_batch((generation, 'https://hrm.test', self.config, [('e-1', '{}')]))
        assert not self.link.sent

    def test_rejection_is_not_delivery(self):
        self._check_in('e-rejected')
        uplink = self._uplink()
        uplink.kick()
        assert wait_for(lambda: self.link.sent)
        self.link.attendanceAck.emit('e-rejected', 'rejected')
        assert wait_for(lambda: not self.repo.pending_outbox())
        row = self.repo.connection.execute('SELECT synced_at, delivery_status FROM attendance_outbox').fetchone()
        assert row['synced_at'] is None
        assert row['delivery_status'] == 'rejected'
