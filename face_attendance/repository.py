"""SQLite is the attendance authority; one connection per persistence worker."""
import json
import sqlite3
import time
from datetime import datetime, timezone

from .catalog import legacy_employee_id
from .models import SaveResult


def utc_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds")


def parse_legacy_time(value):
    # Existing timestamps are naive local times; datetime.timestamp preserves that meaning.
    return datetime.fromisoformat(value).timestamp()


class AttendanceRepository:
    def __init__(self, path, employee_map=None, cooldown_sec=30.0, clock=time.time):
        self.employee_map = employee_map or {}
        self.cooldown_sec, self.clock = cooldown_sec, clock
        self.connection = sqlite3.connect(str(path), timeout=1.5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self._migrate()
        except Exception:
            self.connection.close()
            raise

    def _employee_id(self, name):
        employee = self.employee_map.get(name)
        return employee.employee_id if employee else legacy_employee_id(name)

    def _migrate(self):
        conn = self.connection
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                timestamp TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT, duration REAL)""")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(attendance)")}
            for name, kind in (("employee_id", "TEXT"), ("event_id", "TEXT"),
                               ("captured_at_epoch", "REAL"), ("recorded_at_epoch", "REAL")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE attendance ADD COLUMN {name} {kind}")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS attendance_event_id ON attendance(event_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS attendance_employee_time ON attendance(employee_id, recorded_at_epoch)")
            conn.execute("""CREATE TABLE IF NOT EXISTS attendance_cooldowns (
                employee_id TEXT PRIMARY KEY, last_capture_epoch REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS attendance_outbox (
                event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                created_at TEXT NOT NULL, synced_at TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                actor TEXT DEFAULT 'system')""")
            for row in conn.execute("SELECT id, name, timestamp FROM attendance WHERE employee_id IS NULL").fetchall():
                employee_id = self._employee_id(row["name"])
                try:
                    timestamp = parse_legacy_time(row["timestamp"])
                except (ValueError, TypeError):
                    timestamp = 0.0
                conn.execute("""UPDATE attendance SET employee_id=?, event_id=?,
                    captured_at_epoch=?, recorded_at_epoch=? WHERE id=?""",
                             (employee_id, f"legacy-{row['id']}", timestamp, timestamp, row["id"]))
            # Explicit mappings may be added after local IDs were already used. Preserve
            # attendance and eligibility when an alias is resolved or a local ID becomes an HRM ID.
            remapped = {}
            for name, employee in self.employee_map.items():
                previous_id = legacy_employee_id(name)
                if previous_id == employee.employee_id:
                    continue
                remapped[previous_id] = employee
                conn.execute("UPDATE attendance SET employee_id=? WHERE employee_id=?",
                             (employee.employee_id, previous_id))
                prior = conn.execute("SELECT last_capture_epoch FROM attendance_cooldowns WHERE employee_id=?",
                                     (previous_id,)).fetchone()
                if prior:
                    self._set_cooldown(employee.employee_id, prior[0])
            for row in conn.execute("SELECT event_id, payload FROM attendance_outbox WHERE synced_at IS NULL").fetchall():
                payload = json.loads(row["payload"])
                employee = remapped.get(payload.get("employeeId"))
                if employee:
                    payload["employeeId"], payload["employeeName"] = employee.employee_id, employee.name
                    conn.execute("UPDATE attendance_outbox SET payload=? WHERE event_id=?",
                                 (json.dumps(payload, ensure_ascii=False), row["event_id"]))
            legacy = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_cooldown'").fetchone()
            if legacy:
                for row in conn.execute("SELECT name, last_capture FROM capture_cooldown"):
                    try:
                        epoch = parse_legacy_time(row["last_capture"])
                    except (ValueError, TypeError):
                        continue
                    self._set_cooldown(self._employee_id(row["name"]), epoch)
            # Includes old rows where attendance was committed but the old cooldown update failed.
            for row in conn.execute("""SELECT employee_id, MAX(recorded_at_epoch) AS epoch
                                       FROM attendance GROUP BY employee_id""").fetchall():
                self._set_cooldown(row["employee_id"], row["epoch"] or 0.0)

    def _set_cooldown(self, employee_id, epoch):
        self.connection.execute("""INSERT INTO attendance_cooldowns VALUES (?, ?)
            ON CONFLICT(employee_id) DO UPDATE SET
            last_capture_epoch=MAX(last_capture_epoch, excluded.last_capture_epoch)""", (employee_id, epoch))

    def load_cooldowns(self):
        return {row["employee_id"]: row["last_capture_epoch"] for row in
                self.connection.execute("SELECT employee_id, last_capture_epoch FROM attendance_cooldowns")}

    def record(self, job, snapshots):
        """Serialize competing writers, check eligibility, then commit row + cooldown + outbox."""
        conn, snapshot = self.connection, ""
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                prior = conn.execute("SELECT * FROM attendance WHERE event_id=?", (job.event_id,)).fetchone()
                if prior:
                    return SaveResult(job, "saved", prior["recorded_at_epoch"], prior["snapshot"])
                now = self.clock()
                last = conn.execute("SELECT last_capture_epoch FROM attendance_cooldowns WHERE employee_id=?",
                                    (job.employee_id,)).fetchone()
                if last and now - last[0] < self.cooldown_sec:
                    return SaveResult(job, "duplicate", last[0])
                snapshot = snapshots.save(job)
                conn.execute("""INSERT INTO attendance
                    (employee_id, event_id, name, timestamp, status, snapshot, duration,
                     captured_at_epoch, recorded_at_epoch) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             (job.employee_id, job.event_id, job.employee_name, utc_iso(job.captured_at),
                              "KNOWN", snapshot, round(job.duration, 2), job.captured_at, now))
                self._set_cooldown(job.employee_id, now)
                payload = {"eventId": job.event_id, "employeeId": job.employee_id,
                           "employeeName": job.employee_name, "capturedAt": utc_iso(job.captured_at),
                           "recordedAt": utc_iso(now), "status": "KNOWN", "snapshotPath": snapshot,
                           "verifiedPresenceSeconds": round(job.duration, 2)}
                conn.execute("INSERT INTO attendance_outbox(event_id, payload, created_at) VALUES (?, ?, ?)",
                             (job.event_id, json.dumps(payload, ensure_ascii=False), utc_iso(now)))
            return SaveResult(job, "saved", now, snapshot)
        except Exception as exc:
            # Rollback has completed. Only remove the image created by this exact job.
            if snapshot:
                try:
                    snapshots.remove(snapshot)
                except OSError:
                    pass  # A crash/orphan cleanup can be performed independently of attendance.
            return SaveResult(job, "error", error=str(exc))

    def pending_outbox(self, limit=100):
        return [json.loads(row[0]) for row in self.connection.execute(
            "SELECT payload FROM attendance_outbox WHERE synced_at IS NULL ORDER BY created_at LIMIT ?", (limit,))]

    def mark_synced(self, event_id):
        """Call only after the backend acknowledges eventId (its idempotency key)."""
        with self.connection:
            self.connection.execute("UPDATE attendance_outbox SET synced_at=? WHERE event_id=?",
                                    (utc_iso(self.clock()), event_id))

    def audit(self, action, detail="", actor="system"):
        try:
            self.connection.execute(
                "INSERT INTO audit_log(timestamp, action, detail, actor) VALUES (?, ?, ?, ?)",
                (utc_iso(self.clock()), action, detail, actor))
            self.connection.commit()
        except Exception:
            logging.debug("Audit log write failed", exc_info=True)

    def checkpoint(self):
        try:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logging.exception("WAL checkpoint failed")

    def close(self):
        self.checkpoint()
        self.connection.close()
