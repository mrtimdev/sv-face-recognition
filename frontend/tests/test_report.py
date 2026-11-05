"""Read-only attendance queries: filtering, paging, CSV export, outbox counts.

The reader must never write: it opens with ``PRAGMA query_only=ON`` and every
query degrades to empty results on missing tables instead of raising.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from face_attendance.report import (AttendanceReader, PRESETS, day_start, export_rows,
                                    local_text, preset_range, punctuality)

SCHEMA = """
CREATE TABLE attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    timestamp TEXT NOT NULL, status TEXT NOT NULL, snapshot TEXT, duration REAL,
    employee_id TEXT, event_id TEXT, captured_at_epoch REAL, recorded_at_epoch REAL);
CREATE TABLE attendance_outbox (
    event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
    created_at TEXT NOT NULL, synced_at TEXT);
"""


def seed(path, rows, outbox=()):
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            """INSERT INTO attendance
               (employee_id, event_id, name, timestamp, status, snapshot, duration,
                captured_at_epoch, recorded_at_epoch)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        connection.executemany(
            "INSERT INTO attendance_outbox(event_id, payload, created_at, synced_at)"
            " VALUES (?, '{}', '2026-01-01T00:00:00Z', ?)",
            [(event, synced) for event, synced in outbox])
        connection.commit()
    finally:
        connection.close()


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "attendance.db"
        midnight = day_start()
        self.today = [("EMP-1", "e1", "Alice", "2026-01-01T08:00:00", "PRESENT",
                       "a.jpg", 1.5, midnight + 300, midnight + 305),
                      ("EMP-2", "e2", "José Müller", "2026-01-01T08:05:00", "PRESENT",
                       "", 0.5, midnight + 900, midnight + 905),
                      ("EMP-1", "e3", "Alice", "2026-01-02T08:10:00", "PRESENT",
                       "c.jpg", 2.0, midnight + 90000, midnight + 90005)]
        seed(self.path, self.today, outbox=(("e1", None), ("e2", "2026-01-01T09:00:00Z")))
        self.reader = AttendanceReader(self.path)

    def tearDown(self):
        self.reader.close()
        self.directory.cleanup()

    def test_records_returns_newest_first(self):
        rows = self.reader.records()
        self.assertEqual([row["event_id"] for row in rows], ["e3", "e2", "e1"])
        self.assertEqual(rows[0]["name"], "Alice")
        self.assertEqual(rows[1]["name"], "José Müller")

    def test_time_range_is_start_inclusive_and_end_exclusive(self):
        midnight = day_start()
        self.assertEqual(self.reader.count(start=midnight + 300, end=midnight + 906), 2)
        self.assertEqual(self.reader.count(start=midnight + 306, end=midnight + 906), 1)
        self.assertEqual(self.reader.count(end=midnight + 306), 1)

    def test_employee_and_search_filters(self):
        self.assertEqual(self.reader.count(employee_id="EMP-1"), 2)
        self.assertEqual(self.reader.count(search="José"), 1)
        self.assertEqual(self.reader.count(search="emp-2"), 1)
        self.assertEqual(self.reader.count(search="nobody"), 0)

    def test_paging(self):
        self.assertEqual([r["event_id"] for r in self.reader.records(limit=1)], ["e3"])
        self.assertEqual([r["event_id"] for r in self.reader.records(limit=1, offset=2)],
                         ["e1"])
        self.assertEqual(self.reader.count(), 3)

    def test_summary_and_daily(self):
        summary = self.reader.summary()
        self.assertEqual(summary["records"], 3)
        self.assertEqual(summary["employees"], 2)
        self.assertEqual(summary["days"], 2)
        self.assertAlmostEqual(summary["duration"], 4.0)
        daily = self.reader.daily()
        self.assertEqual([row["records"] for row in daily], [1, 2])
        self.assertTrue(daily[0]["day"] > daily[1]["day"])

    def test_employee_totals_group_by_id(self):
        totals = self.reader.employee_totals()
        self.assertEqual(totals[0]["employee_id"], "EMP-1")

    def test_outbox_counts(self):
        self.assertEqual(self.reader.outbox_counts(), {"pending": 1, "synced": 1})

    def test_legacy_row_falls_back_to_captured_at(self):
        connection = sqlite3.connect(self.path)
        connection.execute(
            """INSERT INTO attendance (name, timestamp, status, captured_at_epoch)
               VALUES ('Bob', 'x', 'PRESENT', ?)""", (day_start() + 7200,))
        connection.commit()
        connection.close()
        self.assertEqual(self.reader.count(), 4)

    def test_missing_tables_degrade_to_empty_results(self):
        empty = Path(self.directory.name) / "blank.db"
        blank = sqlite3.connect(empty)
        blank.close()
        reader = AttendanceReader(empty)
        try:
            self.assertTrue(reader.available)
            self.assertEqual(reader.records(), [])
            self.assertEqual(reader.summary()["records"], 0)
        finally:
            reader.close()

    def test_missing_file_is_unavailable_but_quiet(self):
        reader = AttendanceReader(Path(self.directory.name) / "nope.db")
        try:
            self.assertFalse(reader.available)
            self.assertEqual(reader.records(), [])
        finally:
            reader.close()

    def test_reader_cannot_write(self):
        self.assertEqual(self.reader.connection.execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(sqlite3.OperationalError):
            self.reader.connection.execute("DELETE FROM attendance")

    def test_export_csv_with_utf8_and_punctuality(self):
        rows = self.reader.records(employee_id="EMP-2")
        target = Path(self.directory.name) / "export.csv"
        self.assertEqual(export_rows(target, rows, work_start="00:10",
                                     punctuality_column=True), 1)
        text = target.read_text(encoding="utf-8-sig")
        self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))  # Excel BOM
        self.assertIn("José Müller", text)
        self.assertIn("LATE", text)


class HelperTests(unittest.TestCase):
    def test_punctuality_labels(self):
        midnight = day_start()
        self.assertEqual(punctuality(midnight + 9 * 3600 + 60, "09:00"), "LATE")
        self.assertEqual(punctuality(midnight + 9 * 3600, "09:00"), "ON TIME")
        self.assertEqual(punctuality(midnight, ""), "")
        self.assertEqual(punctuality(None, "09:00"), "")

    def test_presets_are_complete(self):
        self.assertEqual(set(PRESETS), {"today", "hour", "week", "month", "all"})
        for preset in PRESETS:
            start, end = preset_range(preset)
            if preset != "all":
                self.assertIsNotNone(start)
            self.assertTrue(end is None or end > (start or 0))

    def test_local_text_handles_garbage(self):
        self.assertEqual(local_text(0), "")
        self.assertEqual(local_text(None), "")
        self.assertEqual(local_text("not-a-number"), "")
        self.assertNotEqual(local_text(day_start() + 1), "")


if __name__ == "__main__":
    unittest.main()

