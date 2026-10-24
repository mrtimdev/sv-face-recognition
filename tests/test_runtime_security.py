"""Failure recovery and resource bounds for unattended desktop operation."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

import numpy as np

from face_attendance.config import Config
from face_attendance.models import CaptureJob, ObservationEvent, SaveResult
from face_attendance.persistence import PersistenceWorker
from face_attendance.settings import hash_pin, verify_pin, describe_source
from face_attendance.users import UserRepository, hash_password, verify_password


class RuntimeSecurityTests(unittest.TestCase):
    def test_password_and_pin_legacy_compatibility_and_upgrade(self):
        salt = bytes(range(16))
        legacy = salt.hex() + ':' + hashlib.pbkdf2_hmac('sha256', b'secret', salt, 100_000).hex()
        self.assertTrue(verify_password('secret', legacy))
        self.assertFalse(verify_password('wrong', legacy))
        current = hash_password('secret')
        self.assertTrue(current.startswith('pbkdf2_sha256$600000$'))
        self.assertTrue(verify_password('secret', current))
        self.assertFalse(verify_password('wrong', current))
        for malformed in ('bad', 'pbkdf2_sha256$9999999999$00$00', None):
            self.assertFalse(verify_password('secret', malformed))
        self.assertTrue(verify_pin('1234', hashlib.sha256(b'1234').hexdigest()))
        self.assertTrue(verify_pin('1234', hash_pin('1234')))
        with tempfile.TemporaryDirectory() as directory:
            repo = UserRepository(Path(directory) / 'test.db')
            user = repo.create_user('test', 'Test', '', '', 'secret')
            connection = repo._connect()
            connection.execute('UPDATE users SET password_hash=? WHERE uuid=?', (legacy, user.uuid))
            connection.commit()
            connection.close()
            signed_in = repo.authenticate('test', 'secret')
            self.assertTrue(signed_in.password_hash.startswith('pbkdf2_sha256$600000$'))
            self.assertEqual(repo.get_user(user.uuid).password_hash, signed_in.password_hash)

    def test_session_write_is_private_atomic_and_old_file_survives_failure(self):
        from face_attendance.users import save_session
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.session'
            user = Mock(uuid='id', username='test', full_name='Test', role='user')
            with patch('face_attendance.users.SESSION_PATH', path):
                save_session(user)
                self.assertEqual(json.loads(path.read_text())['user_uuid'], 'id')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                before = path.read_bytes()
                with patch('face_attendance.users.os.replace', side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):
                        save_session(user)
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_source_labels_redact_password_query_and_fragment(self):
        value = describe_source('rtsp://operator:secret@camera/live?token=private#another-secret')
        self.assertNotIn('secret', value)
        self.assertNotIn('private', value)
        self.assertIn('camera/live', value)
        self.assertNotIn('private', describe_source('https://camera/live?token=private'))

    def test_account_database_outage_fails_closed_even_without_marker(self):
        from face_attendance.users import account_login_required
        repository = Mock()
        repository.has_users.side_effect = OSError('unreachable database')
        with patch('face_attendance.users.remember_accounts_exist') as marker:
            self.assertTrue(account_login_required(repository))
            marker.assert_not_called()
        repository.has_users.side_effect = None
        repository.has_users.return_value = False
        self.assertFalse(account_login_required(repository))

    def test_malformed_and_expired_session_fail_closed(self):
        from face_attendance.users import save_session, load_session
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.session'
            with patch('face_attendance.users.SESSION_PATH', path):
                for value in ('[]', '{}', 'null', 'x' * 70_000):
                    path.write_text(value)
                    self.assertIsNone(load_session())
                user = Mock(uuid='id', username='test', full_name='Test', role='user')
                save_session(user)
                self.assertIsNotNone(load_session())
                payload = json.loads(path.read_text())
                payload['created_at'] = '2000-01-01T00:00:00+00:00'
                path.write_text(json.dumps(payload))
                self.assertIsNone(load_session())

    def test_sleep_assertion_is_scoped_idempotent_and_released(self):
        from face_attendance.power import SleepInhibitor
        inhibitor = SleepInhibitor()
        process = Mock()
        with patch('face_attendance.power.sys.platform', 'darwin'), \
                patch('face_attendance.power.subprocess.Popen', return_value=process) as spawn:
            inhibitor.acquire()
            inhibitor.acquire()
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(spawn.call_args.args[0][:3], ['/usr/bin/caffeinate', '-i', '-w'])
            inhibitor.release()
            inhibitor.release()
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=2)
            self.assertIsNone(inhibitor.process)

    def test_notification_reconfiguration_does_not_reroute_face_photos(self):
        from face_attendance.dashboard.telegram import TelegramService
        with patch.object(TelegramService, '_start_worker'), \
                patch('face_attendance.dashboard.telegram._HAS_REQUESTS', True):
            service = TelegramService('old-token', 'old-chat')
            service.send_capture('Employee', 'id', 3.)
            queued = service._queue.get_nowait()
            service.reconfigure('new-token', 'new-chat')
            with patch('face_attendance.dashboard.telegram.requests.post') as post:
                service._send(queued)
                post.assert_not_called()
            service.stop()

    def test_notification_error_does_not_expose_bot_token(self):
        from face_attendance.dashboard.telegram import TelegramService
        with patch.object(TelegramService, '_start_worker'), \
                patch('face_attendance.dashboard.telegram._HAS_REQUESTS', True):
            service = TelegramService('secret-bot-token', 'chat')
            with patch('face_attendance.dashboard.telegram.requests.get',
                       side_effect=OSError('https://api.telegram.org/botsecret-bot-token/getMe')):
                message = service.send_test()
                self.assertNotIn('secret-bot-token', message)
                self.assertIn('Connection failed', message)
            service.stop()

    def test_pending_notifications_honor_opt_out_without_destination_change(self):
        from face_attendance.dashboard.telegram import TelegramService
        with patch.object(TelegramService, '_start_worker'), \
                patch('face_attendance.dashboard.telegram._HAS_REQUESTS', True):
            service = TelegramService('token', 'chat')
            service.send_capture('Employee', 'id', 3.)
            queued = service._queue.get_nowait()
            service.reconfigure('token', 'chat', notify_capture=False)
            with patch('face_attendance.dashboard.telegram.requests.post') as post:
                service._send(queued)
                post.assert_not_called()
            service.stop()

    def test_soak_final_health_gate_rejects_late_error_and_unavailable_storage(self):
        from scripts.soak_runtime import validate_runtime
        engine = Mock(running=True, stats={'storage_ready': True})
        for name in ('camera', 'recognition', 'persistence'):
            getattr(engine, name).thread.is_alive.return_value = True
        validate_runtime(engine, [], require_storage=True)
        with self.assertRaisesRegex(RuntimeError, 'runtime error'):
            validate_runtime(engine, ['late error'], require_storage=True)
        engine.stats['storage_ready'] = False
        with self.assertRaisesRegex(RuntimeError, 'storage'):
            validate_runtime(engine, [], require_storage=True)

    def test_observation_rotation_bounds_files_and_retains_csv_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'observations.csv'
            worker = PersistenceWorker(replace(Config(), log_path=path), {}, audio_enabled=False)
            with patch('face_attendance.persistence.OBSERVATION_LOG_BYTES', 1):
                for index in range(20):
                    worker._observe(ObservationEvent(f'Employee {index}', time.time(), False))
            files = list(Path(directory).iterdir())
            self.assertEqual(len(files), 4)
            for file in files:
                self.assertTrue(file.read_text().startswith('timestamp,name,status'))
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertIn('Employee 19', path.read_text())

    def test_storage_reconnects_after_record_failure_and_optional_audio_failure(self):
        connected = []
        closed = []

        class Repository:
            def __init__(self, *args):
                self.index = len(connected)
                connected.append(self)

            def verified_today(self):
                return {}

            def load_cooldowns(self):
                return {}

            def record(self, job, snapshots):
                if self.index == 0:
                    raise OSError('database disconnected')
                return SaveResult(job, 'saved', time.time(), '')

            def audit(self, *args):
                pass

            def close(self):
                closed.append(self)

        worker = PersistenceWorker(replace(Config(), retry_sec=.01), {}, Repository, audio_enabled=False)
        job = CaptureJob('one', 1, 'id', 'Employee', time.time(), 3., np.zeros((20, 20, 3), np.uint8))
        with patch.object(worker, '_init_audio', side_effect=OSError('audio unavailable')):
            worker.start()
            try:
                self.assertTrue(worker.startup.ready.wait(2))
                self.assertTrue(worker.submit(job))
                self.assertEqual(worker.results.get(timeout=2).outcome, 'error')
                self.assertTrue(worker.submit(replace(job, event_id='two')))
                self.assertEqual(worker.results.get(timeout=2).outcome, 'saved')
                self.assertEqual(len(connected), 2)
                self.assertIn(connected[0], closed)
            finally:
                worker.close()
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(worker.jobs.unfinished_tasks, 0)


class DesktopBackpressureTests(unittest.TestCase):
    def test_busy_gui_receives_only_newest_frame_and_stats(self):
        from tests.test_dashboard import _app, temp_settings
        from tests.test_pipeline import FakeBackend
        from face_attendance.dashboard.engine import AttendanceEngine
        application = _app()
        with tempfile.TemporaryDirectory() as directory:
            engine = AttendanceEngine(temp_settings(directory), backend=FakeBackend(), use_lock=False)
            frames, stats = [], []
            engine.frameReady.connect(frames.append)
            engine.statsReady.connect(stats.append)
            old = np.zeros((32, 32, 3), np.uint8)
            newest = np.ones_like(old)
            def producer():
                for index in range(10_000):
                    engine._queue_preview(frame=old, stats={'index': index})
                engine._queue_preview(frame=newest)
            thread = threading.Thread(target=producer)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(frames, [])
            self.assertEqual(len(engine._preview_values), 2)
            application.processEvents()
            self.assertEqual(len(frames), 1)
            self.assertIs(frames[0], newest)
            self.assertEqual(stats, [{'index': 9999}])
            self.assertFalse(engine._preview_pending)
            engine.shutdown()

    def test_restart_refuses_a_previous_stuck_native_worker(self):
        from tests.test_dashboard import _app, temp_settings
        from tests.test_pipeline import FakeBackend
        from face_attendance.dashboard.engine import AttendanceEngine
        _app()
        with tempfile.TemporaryDirectory() as directory:
            engine = AttendanceEngine(temp_settings(directory), backend=FakeBackend(), use_lock=False)
            for name in ('camera', 'recognition', 'persistence'):
                with self.subTest(worker=name):
                    original = getattr(engine, name)
                    with patch.object(getattr(engine, name).thread, 'is_alive', return_value=True):
                        with patch.object(engine, '_build') as build:
                            with self.assertRaisesRegex(RuntimeError, 'previous ' + name):
                                engine.start()
                            build.assert_not_called()
                        with self.assertRaisesRegex(RuntimeError, 'previous ' + name):
                            engine.reload_catalog()
                        self.assertIs(getattr(engine, name), original)
            engine.shutdown()
