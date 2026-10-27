"""Employment lifecycle, attendance denial and per-person blocked scan evidence."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np

from face_attendance.catalog import FaceCatalog, load_employee_map
from face_attendance.enrollment import EnrollmentService, write_encodings, write_employee
from face_attendance.models import Detection, Employee, FramePacket, RecognitionResult, State, CaptureJob
from face_attendance.tracking import FaceTracker
from face_attendance.config import Config
from face_attendance.attendance import AttendanceService
from face_attendance.repository import AttendanceRepository
from face_attendance.storage import SnapshotService
from face_attendance.transfer import export_employees, import_employees
from tests.test_attendance import FakePersistence, verified
from tests.test_dashboard import _app, temp_settings


class EmploymentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.encodings = self.root / 'faces.pickle'
        self.employees = self.root / 'employees.json'
        write_encodings(self.encodings, {'Alice': [np.eye(128, dtype=np.float32)[0]],
                                         'Alias': [np.eye(128, dtype=np.float32)[0]]})
        write_employee(self.employees, 'Alice', 'A')
        write_employee(self.employees, 'Alias', 'A')
        self.service = EnrollmentService(self.encodings, self.employees)

    def tearDown(self):
        self.directory.cleanup()

    def test_defaults_alias_deactivation_and_edits_preserve_status(self):
        self.assertTrue(all(e.active for e in load_employee_map(self.employees).values()))
        before = self.encodings.read_bytes()
        self.service.set_employee_active('Alice', False)
        self.service.update_employee('Alice', 'Alice Updated')
        write_employee(self.employees, 'Alice', 'A')
        write_employee(self.employees, 'New alias', 'A')
        self.assertTrue(all(not e.active for e in load_employee_map(self.employees).values()))
        self.assertEqual(before, self.encodings.read_bytes())
        self.assertTrue(all(not e.active for e in FaceCatalog(self.encodings, self.employees).employees))
        self.service.set_employee_active('Alias', True)
        self.assertTrue(all(e.active for e in load_employee_map(self.employees).values()))

    def test_legacy_records_default_active_and_bad_status_is_rejected(self):
        self.employees.write_text(json.dumps({'Alice': {'employee_id': 'A'}}))
        self.assertTrue(load_employee_map(self.employees)['Alice'].active)
        self.employees.write_text(json.dumps({'Alice': {'employee_id': 'A', 'status': 'maybe'}}))
        with self.assertRaises(ValueError):
            load_employee_map(self.employees)

    def test_unmapped_legacy_alias_cannot_bypass_inactive_id(self):
        from face_attendance.catalog import legacy_employee_id
        from face_attendance.enrollment import employee_rows
        self.employees.write_text(json.dumps({'Alias': {
            'employee_id': legacy_employee_id('Alice'), 'status': 'inactive'}}))
        self.assertTrue(all(not e.active for e in FaceCatalog(self.encodings, self.employees).employees))
        self.assertTrue(all(not r['active'] for r in employee_rows(self.encodings, self.employees)))
        # Preserve the restriction even when the mapped alias has no templates to export.
        write_encodings(self.encodings, {'Alice': [np.eye(128, dtype=np.float32)[0]]})
        export_employees(self.root / 'legacy.zip', self.encodings, self.employees)
        import_employees(self.root / 'legacy.zip', self.root / 'out.pickle', self.root / 'out.json')
        self.assertFalse(load_employee_map(self.root / 'out.json')['Alice'].active)

    def test_export_import_preserves_inactive_status(self):
        self.service.set_employee_active('Alice', False)
        archive = self.root / 'employees.zip'
        export_employees(archive, self.encodings, self.employees)
        target = self.root / 'target'
        target.mkdir()
        import_employees(archive, target / 'faces.pickle', target / 'employees.json')
        self.assertTrue(all(not e.active for e in load_employee_map(target / 'employees.json').values()))

    def test_commit_rechecks_new_status_and_preserves_history(self):
        config = replace(Config(), db_path=self.root / 'attendance.db', employees_path=self.employees)
        repo = AttendanceRepository(config, load_employee_map(self.employees), cooldown_sec=0)
        snapshots = SnapshotService(self.root / 'captures')
        frame = np.zeros((40, 40, 3), np.uint8)
        job = CaptureJob('before', 1, 'A', 'Alice', 1000, 3, frame)
        try:
            self.assertEqual(repo.record(job, snapshots).outcome, 'saved')
            self.service.set_employee_active('Alice', False)
            result = repo.record(replace(job, event_id='blocked'), snapshots)
            self.assertEqual(result.outcome, 'blocked')
            self.assertEqual(repo.connection.execute('SELECT COUNT(*) FROM attendance').fetchone()[0], 1)
            self.assertEqual(len(repo.pending_outbox()), 1)
            self.service.set_employee_active('Alice', True)
            self.assertEqual(repo.record(replace(job, event_id='reactivated'), snapshots).outcome, 'saved')
        finally:
            repo.close()


class InactiveMonitoringTests(unittest.TestCase):
    def test_tracker_recognizes_inactive_and_attendance_never_submits(self):
        tracker = FaceTracker(Config())
        employee = Employee('A', 'Alice', False)
        box = (30, 170, 180, 30)
        for i in range(4):
            now = 1 + i * .2
            packet = FramePacket(i * 6, now, 1000 + now, 1,
                                 np.random.default_rng(7).integers(0, 255, (240, 320, 3), np.uint8))
            tracker.advance(packet)
            self.assertTrue(tracker.apply(RecognitionResult(packet, (Detection(box, employee, .2, True),), .01), now))
        track = tracker.tracks[1]
        self.assertTrue(track.identity_valid)
        self.assertFalse(track.employee_active)
        self.assertEqual(track.state, State.INACTIVE)
        self.assertEqual(track.verified_presence, 0)
        self.assertIs(track.unknown_packet, packet)
        worker = FakePersistence()
        attendance = AttendanceService(Config(), worker)
        worker.startup.put(({}, ''))
        attendance.poll({}, now, 1000)
        track.verified_presence = 10
        self.assertEqual(attendance.update(tracker.tracks, packet, now, True), [])
        attendance.observe(tracker.tracks, now, 1000)
        self.assertFalse(worker.jobs)
        self.assertFalse(worker.events)

    def test_saved_inactive_capture_is_separate_and_gated(self):
        from face_attendance.dashboard.engine import AttendanceEngine
        from tests.test_pipeline import FakeBackend
        _app()
        with tempfile.TemporaryDirectory() as root:
            engine = AttendanceEngine(temp_settings(root), backend=FakeBackend(), use_lock=False)
            track = verified(employee_id='A')
            track.employee_active = False
            track.state = State.INACTIVE
            track.state_since = 8
            track.unknown_packet = track.evidence_packet
            track.unknown_box = track.evidence_box
            track.spoof_ok = track.liveness_ok = False
            engine.tracker.tracks[1] = track
            alerts = []
            engine.inactiveEmployeeAlert.connect(alerts.append)
            for status, paused, generation in [('CONNECTED', True, 1), ('DISCONNECTED', False, 1),
                                               ('CONNECTED', False, 2)]:
                engine._capture_observations(10, generation, status, paused)
                self.assertFalse(alerts)
            engine.settings.auto_capture_inactive = False
            engine._capture_observations(10, 1, 'CONNECTED', False)
            self.assertFalse(alerts)
            engine.settings.auto_capture_inactive = True
            engine._capture_observations(10, 1, 'CONNECTED', False)
            engine._capture_observations(10, 1, 'CONNECTED', False)
            self.assertEqual(len(alerts), 1)
            info = alerts[0]
            self.assertEqual(info['employee_id'], 'A')
            self.assertIn('/inactive/', info['snapshot'])
            saved = json.loads(Path(info['snapshot']).with_name('record.json').read_text())
            self.assertTrue(saved['attendance_blocked'])
            self.assertFalse(saved['liveness_verified'])
            self.assertFalse(saved['presentation_verified'])
            self.assertTrue(Path(info['snapshot']).is_file())
            self.assertFalse(list((Path(root) / 'captures').glob('unknown/*')))
            engine.shutdown()

    def test_unknown_alert_does_not_suppress_later_inactive_match_on_same_track(self):
        from face_attendance.dashboard.engine import AttendanceEngine
        from tests.test_pipeline import FakeBackend
        _app()
        with tempfile.TemporaryDirectory() as root:
            engine = AttendanceEngine(temp_settings(root), backend=FakeBackend(), use_lock=False)
            track = verified(employee_id='A')
            track.state = State.UNKNOWN
            track.state_since = 7
            track.identity_valid = False
            track.unknown_packet = track.evidence_packet
            track.unknown_box = track.evidence_box
            engine.tracker.tracks[1] = track
            unknown, inactive = [], []
            engine.unknownFaceAlert.connect(unknown.append)
            engine.inactiveEmployeeAlert.connect(inactive.append)
            engine._capture_observations(10, 1, 'CONNECTED', False)
            track.employee_active = False
            track.identity_valid = True
            track.state = State.INACTIVE
            engine._capture_observations(10, 1, 'CONNECTED', False)
            self.assertEqual(len(unknown), 1)
            self.assertEqual(len(inactive), 1)
            self.assertNotEqual(unknown[0]['snapshot'], inactive[0]['snapshot'])
            engine.shutdown()

    def test_inactive_sound_is_distinct_and_never_announces_verified(self):
        from face_attendance.sound_events import EventSounds
        from face_attendance.settings import Settings
        from tests.test_sound_events import Player
        clock = [0.]
        sounds = EventSounds(replace(Settings(), alert_path='/missing.wav'), Player, lambda: clock[0])
        sounds.camera(True, True)
        track = dict(track_id=1, visible=True, state='INACTIVE', identity_valid=True,
                     employee_active=False, quality_ok=True, spoof_ok=True, liveness_ok=True)
        sounds.tracks([track])
        clock[0] = 2
        sounds.tracks([track])
        clock[0] = 20
        sounds.tracks([track])
        self.assertEqual(sounds.players['inactive'].plays, 1)
        self.assertEqual(sounds.players['recognized'].plays, 0)
        self.assertEqual(sounds.players['verified'].plays, 0)

    def test_inactive_telegram_uses_its_own_switch_and_caption(self):
        from face_attendance.dashboard.telegram import TelegramService
        with patch.object(TelegramService, '_start_worker'), patch(
                'face_attendance.dashboard.telegram._HAS_REQUESTS', True):
            sender = TelegramService('token', 'chat', notify_unknown=False, notify_capture=False)
            info = dict(employee_name='Alice', employee_id='A', captured_at='UTC', track_id=1,
                        frame=np.zeros((20, 20, 3), np.uint8))
            sender.send_inactive_alert(info)
            job = sender._queue.get_nowait()
            self.assertEqual(job.kind, 'inactive')
            self.assertIn('Attendance Blocked', job.caption)
            self.assertIsNotNone(job.image)
            sender.reconfigure('token', 'chat', notify_inactive=False)
            sender.send_inactive_alert(info)
            self.assertTrue(sender._queue.empty())

    def test_directory_toggle_filter_and_live_activity(self):
        from face_attendance.dashboard.app import MainWindow
        from face_attendance.dashboard.screens.employees import EmployeesScreen
        _app()
        with tempfile.TemporaryDirectory() as root, patch('face_attendance.dashboard.screens.live.EventSounds'):
            settings = temp_settings(root)
            window = MainWindow(settings, use_lock=False)
            try:
                screen = next(s for s in window.screens if isinstance(s, EmployeesScreen))
                screen.reload()
                screen.employee_table.selectRow(0)
                screen._toggle_employee_active()
                self.assertFalse(screen.rows[0]['active'])
                self.assertFalse(window.engine.catalog.employees[0].active)
                screen.status_filter.setCurrentText('Ready for attendance')
                self.assertTrue(screen.employee_table.isRowHidden(0))
                screen.status_filter.setCurrentText('Inactive employees')
                self.assertFalse(screen.employee_table.isRowHidden(0))
                # A delivered observation populates both monitoring screens and notifications.
                info = dict(employee_name='Seed', employee_id=screen.rows[0]['employee_id'],
                            snapshot='', captured_at='UTC', track_id=1,
                            frame=np.zeros((20, 20, 3), np.uint8))
                window.engine.inactiveEmployeeAlert.emit(info)
                self.assertEqual(window.screens[0].activity._rows[0].entry['status'], 'Inactive — blocked')
                screen.employee_table.selectRow(0)
                screen._toggle_employee_active()
                self.assertTrue(screen.rows[0]['active'])
                self.assertTrue(window.engine.catalog.employees[0].active)
            finally:
                window.close()
                _app().processEvents()
