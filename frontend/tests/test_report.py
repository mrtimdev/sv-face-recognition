"""Read-only attendance queries: filtering, paging, CSV export, outbox counts.

The reader must never write: it opens with ``PRAGMA query_only=ON`` and every
query degrades to empty results on missing tables instead of raising.
"""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from face_attendance.report import (AttendanceReader, PRESETS, day_start, delete_observations, export_rows,
                                    local_text, observation_rows, preset_range, punctuality, report_page)

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

    def _observe(self, kind, epoch, name="", code=""):
        import json
        from datetime import datetime, timezone
        folder = Path(self.directory.name) / "captures" / kind / f"ev{epoch:.0f}"
        folder.mkdir(parents=True)
        stamp = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        (folder / "record.json").write_text(json.dumps({
            "event_id": folder.name, "employee_id": code, "employee_name": name, "age": 2.5,
            "captured_at": stamp, "snapshot": str(folder / "face.jpg")}), encoding="utf-8")
        return folder

    def test_status_filters_split_known_not_enrolled_unknown_and_inactive(self):
        midnight = day_start()
        captures = Path(self.directory.name) / "captures"
        self._observe("unknown", midnight + 600)
        self._observe("inactive", midnight + 1000, "Bob", "EMP-9")
        not_enrolled = ("emp-1",)

        def page(status, **kw):
            rows, total, summary = report_page(self.reader, captures, status, not_enrolled, **kw)
            return [(r["event_id"], r["status"]) for r in rows], total, summary

        rows, total, _ = page("known")
        self.assertEqual((rows, total), ([("e2", "PRESENT")], 1))
        rows, total, summary = page("not_enrolled")
        self.assertEqual(rows, [("e3", "NOT ENROLLED"), ("e1", "NOT ENROLLED")])
        self.assertEqual((total, summary["records"]), (2, 2))
        rows, total, _ = page("unknown")
        self.assertEqual((rows, total), ([(f"ev{midnight + 600:.0f}", "UNKNOWN")], 1))
        rows, total, _ = page("inactive", search="bob")
        self.assertEqual(rows[0][1], "INACTIVE")
        rows, total, _ = page("all")
        self.assertEqual(total, 5)
        self.assertEqual([status for _, status in rows],
                         ["NOT ENROLLED", "INACTIVE", "PRESENT", "UNKNOWN", "NOT ENROLLED"])
        rows, total, _ = page("all", limit=2, offset=2)
        self.assertEqual([status for _, status in rows], ["PRESENT", "UNKNOWN"])
        # Nobody marked not enrolled: nothing under that filter, everyone is known.
        self.assertEqual(report_page(self.reader, captures, "not_enrolled", ())[1], 0)
        self.assertEqual(report_page(self.reader, captures, "known", ())[1], 3)

    def test_observations_can_be_deleted_only_inside_the_capture_folder(self):
        captures = Path(self.directory.name) / "captures"
        self._observe("unknown", day_start() + 60)
        rows = observation_rows(captures)
        outside = dict(rows[0], observation_dir=str(Path(self.directory.name)))
        self.assertEqual(delete_observations(captures, [outside]), 0)
        self.assertEqual(delete_observations(captures, rows), 1)
        self.assertEqual(observation_rows(captures), [])

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
        self.assertEqual(self.reader.outbox_counts(), {"pending": 1, "synced": 0, "rejected": 0, "unassigned": 1, "legacy_resolved": 1})

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



class ReportScreenStatusTests(unittest.TestCase):
    def test_status_filter_shows_unknown_and_not_enrolled_rows(self):
        import json
        import os
        from datetime import datetime, timezone
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from face_attendance import hrm_directory as hd
        from face_attendance.dashboard.screens.report import ReportScreen
        from tests.test_dashboard import StubEngine, _app, temp_settings
        _app()
        with tempfile.TemporaryDirectory() as root:
            settings = temp_settings(root)
            now = day_start() + 3600
            seed(settings.db_path, [("EMP0150", "e1", "Nget Tim", "t", "KNOWN", "", 1.0, now, now),
                                    ("EMP0001", "e2", "Dara", "t", "KNOWN", "", 1.0, now + 5, now + 5)])
            row = hd.normalize({"id": 1, "employeeCode": "EMP0150", "firstName": "Tim", "status": "ACTIVE",
                                "faceEnrolled": False, "faceEnrolledMode": "not_enrolled"})
            Path(hd.cache_path(settings)).write_text(json.dumps(
                {"version": hd.CACHE_VERSION, "fetched_at": 1, "source": "x", "employees": [row]}), encoding="utf-8")
            folder = Path(settings.capture_dir) / "unknown" / "u1"
            folder.mkdir(parents=True)
            (folder / "record.json").write_text(json.dumps(
                {"event_id": "u1", "age": 1.0, "captured_at": datetime.fromtimestamp(now + 9, timezone.utc).isoformat()}))
            screen = ReportScreen(StubEngine(), settings)
            try:
                screen.start_edit.setDate(screen.start_edit.date().addDays(-1))
                statuses = lambda: [screen.table.item(i, 5).text() for i in range(screen.table.rowCount())]
                screen.reload(reset_page=True)
                self.assertEqual(statuses(), ["UNKNOWN", "KNOWN", "NOT ENROLLED"])
                for key, expected in (("known", ["KNOWN"]), ("not_enrolled", ["NOT ENROLLED"]),
                                      ("unknown", ["UNKNOWN"]), ("inactive", [])):
                    screen.status_filter.setCurrentIndex(screen.status_filter.findData(key))
                    self.assertEqual(statuses(), expected, key)
            finally:
                screen.close()
