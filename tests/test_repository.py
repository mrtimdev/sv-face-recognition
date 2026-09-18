import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from face_attendance.catalog import legacy_employee_id
from face_attendance.models import CaptureJob, Employee
from face_attendance.repository import AttendanceRepository
from face_attendance.storage import SnapshotService


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "attendance.db"
        self.snapshots = SnapshotService(self.root / "captures")
        self.now = 2000.0
        self.repo = AttendanceRepository(self.db, clock=lambda: self.now)
        self.job = CaptureJob("event-one", 1, "E001", "Test Employee", 1999, 3.1,
                              np.full((64, 64, 3), 57, np.uint8))

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def count(self, table):
        return self.repo.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_atomic_record_outbox_idempotency_and_cooldown(self):
        saved = self.repo.record(self.job, self.snapshots)
        self.assertEqual(saved.outcome, "saved")
        self.assertTrue(Path(saved.snapshot).exists())
        self.assertTrue(np.all(cv2.imread(saved.snapshot) == 57))
        self.assertEqual(self.count("attendance"), 1)
        payload = self.repo.pending_outbox()[0]
        self.assertEqual(payload["employeeId"], "E001")
        self.assertEqual(payload["eventId"], "event-one")
        self.assertTrue(payload["capturedAt"].endswith("+00:00"))
        replay = self.repo.record(self.job, self.snapshots)
        self.assertEqual(replay.snapshot, saved.snapshot)
        duplicate = self.repo.record(replace(self.job, event_id="event-two"), self.snapshots)
        self.assertEqual(duplicate.outcome, "duplicate")
        self.assertEqual(self.count("attendance"), 1)
        self.assertEqual(len(list(self.snapshots.directory.glob("*.jpg"))), 1)
        self.repo.mark_synced("event-one")
        self.assertEqual(self.repo.pending_outbox(), [])
        self.now += 30
        self.assertEqual(self.repo.record(replace(self.job, event_id="event-three"), self.snapshots).outcome, "saved")
        self.assertEqual(self.count("attendance"), 2)

    def test_snapshot_failure_rolls_back_everything(self):
        with patch("face_attendance.storage.cv2.imwrite", return_value=False):
            result = self.repo.record(self.job, self.snapshots)
        self.assertEqual(result.outcome, "error")
        for table in ("attendance", "attendance_cooldowns", "attendance_outbox"):
            self.assertEqual(self.count(table), 0)

    def test_database_failure_removes_new_evidence_and_all_transaction_changes(self):
        self.repo.connection.execute("""CREATE TRIGGER fail_outbox BEFORE INSERT ON attendance_outbox
            BEGIN SELECT RAISE(ABORT, 'simulated DB error'); END""")
        result = self.repo.record(self.job, self.snapshots)
        self.assertEqual(result.outcome, "error")
        for table in ("attendance", "attendance_cooldowns", "attendance_outbox"):
            self.assertEqual(self.count(table), 0)
        self.assertEqual(list(self.snapshots.directory.glob("*.jpg")), [])
        self.repo.connection.execute("DROP TRIGGER fail_outbox")
        self.assertEqual(self.repo.record(self.job, self.snapshots).outcome, "saved")

    def test_restart_loads_persistent_cooldowns(self):
        self.repo.record(self.job, self.snapshots)
        self.repo.close()
        self.repo = AttendanceRepository(self.db, clock=lambda: self.now + 1)
        self.assertEqual(self.repo.load_cooldowns(), {"E001": 2000.0})
        self.assertEqual(self.repo.record(replace(self.job, event_id="restart"), self.snapshots).outcome, "duplicate")

    def test_alias_mapping_preserves_cooldown_and_updates_pending_hrm_event(self):
        old_id = legacy_employee_id("Old Label")
        self.repo.record(replace(self.job, employee_id=old_id, employee_name="Old Label"), self.snapshots)
        self.repo.close()
        self.repo = AttendanceRepository(self.db, {"Old Label": Employee("HRM-7", "Correct Name")},
                                         clock=lambda: self.now + 1)
        self.assertEqual(self.repo.load_cooldowns()["HRM-7"], 2000)
        self.assertEqual(self.repo.pending_outbox()[0]["employeeId"], "HRM-7")
        self.assertEqual(self.repo.pending_outbox()[0]["employeeName"], "Correct Name")
        row = self.repo.connection.execute("SELECT employee_id, name FROM attendance").fetchone()
        self.assertEqual(row["employee_id"], "HRM-7")
        self.assertEqual(row["name"], "Old Label")  # Keep the original historical display name.
        result = self.repo.record(replace(self.job, event_id="new-event", employee_id="HRM-7"), self.snapshots)
        self.assertEqual(result.outcome, "duplicate")

    def test_competing_connections_cannot_double_record(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def writer(event_id):
            try:
                repo = AttendanceRepository(self.db, clock=lambda: 2000)
                try:
                    barrier.wait(timeout=3)
                    results.append(repo.record(replace(self.job, event_id=event_id), self.snapshots).outcome)
                finally:
                    repo.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(str(i),)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertCountEqual(results, ["saved", "duplicate"])
        self.assertEqual(self.count("attendance"), 1)

    def test_legacy_migration_preserves_rows_and_reconciles_cooldown(self):
        path = self.root / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript("""CREATE TABLE attendance (id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, timestamp TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT, duration REAL);
            CREATE TABLE capture_cooldown(name TEXT PRIMARY KEY, last_capture TEXT NOT NULL);""")
        timestamp = datetime.fromtimestamp(1950).isoformat()
        conn.execute("INSERT INTO attendance(name,timestamp,status,snapshot,duration) VALUES(?,?,?,?,?)",
                     ("Legacy", timestamp, "KNOWN", "original.jpg", 3.5))
        conn.execute("INSERT INTO capture_cooldown VALUES (?,?)", ("Legacy", datetime.fromtimestamp(1990).isoformat()))
        conn.commit()
        conn.close()
        repo = AttendanceRepository(path, {"Legacy": Employee("HRM-42", "Legacy")})
        try:
            row = repo.connection.execute("SELECT * FROM attendance").fetchone()
            self.assertEqual(row["snapshot"], "original.jpg")
            self.assertEqual(row["employee_id"], "HRM-42")
            self.assertEqual(repo.load_cooldowns(), {"HRM-42": 1990})
            self.assertNotEqual(legacy_employee_id("Legacy"), "HRM-42")
            self.assertEqual(repo.pending_outbox(), [])  # Historical rows are not silently re-sent.
        finally:
            repo.close()
