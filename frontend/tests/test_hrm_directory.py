"""HRM employee directory: download, cache, merge with enrollments, filter, paginate."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from face_attendance import hrm_directory as hd
from face_attendance import hrm_sync as hrm
from face_attendance.hrm_sync import HRMConfig, HRMError
from tests.test_hrm_sync import CONFIG, FakeHTTP, KEY, _Response, header, http_error


def api_row(code, first, last, department="IT", position="Developer", status="ACTIVE", **extra):
    row = {"id": code, "employeeCode": code, "firstName": first, "middleName": None,
           "lastName": last, "departmentName": department, "positionName": position,
           "locationName": "Phnom Penh", "costCenterName": "HQ", "status": status,
           "terminated": False, "hasPhoto": False}
    row.update(extra)
    return row


def page(rows, number, total_pages, total):
    return {"success": True, "data": {"employees": rows, "page": number, "totalPages": total_pages,
                                      "totalElements": total}}


class FetchTests(unittest.TestCase):
    def test_fetch_all_walks_every_page_with_the_key_in_a_header(self):
        http = FakeHTTP(page([api_row("EMP001", "A", "One")], 0, 2, 2),
                        page([api_row("EMP002", "B", "Two")], 1, 2, 2))
        progress = []
        rows = hrm.fetch_all_employees(CONFIG, opener=http, progress=lambda d, t: progress.append((d, t)))
        self.assertEqual([row["employeeCode"] for row in rows], ["EMP001", "EMP002"])
        self.assertEqual(progress, [(1, 2), (2, 2)])
        first, follow = http.requests[0]
        self.assertTrue(first.full_url.startswith("https://hrm.example.com/api/face-recognition/employees?"))
        self.assertIn("page=0", first.full_url)
        self.assertIn("status=ACTIVE", first.full_url)
        self.assertIn("size=500", first.full_url)
        self.assertNotIn(KEY, first.full_url)
        self.assertEqual(header(first, "X-API-KEY"), KEY)
        self.assertFalse(follow)
        self.assertIn("page=1", http.requests[1][0].full_url)

    def test_fetch_stops_on_an_empty_page(self):
        http = FakeHTTP(page([], 0, 5, 0))
        self.assertEqual(hrm.fetch_all_employees(CONFIG, opener=http), [])
        self.assertEqual(len(http.requests), 1)

    def test_photo_404_means_no_photo_and_other_errors_raise(self):
        self.assertIsNone(hrm.employee_photo(CONFIG, "EMP001", opener=FakeHTTP(http_error(404))))
        with self.assertRaises(HRMError):
            hrm.employee_photo(CONFIG, "EMP001", opener=FakeHTTP(http_error(401)))

    def test_photo_is_cached_only_when_it_is_an_image(self):
        class Raw:
            def __init__(self, data):
                self.data = data

            def __call__(self, request, timeout, follow_redirects=False):
                return _Response(self.data)

        with tempfile.TemporaryDirectory() as root:
            settings = type("S", (), {"employees_path": str(Path(root) / "employees.json")})()
            self.assertIsNone(hd.fetch_photo(CONFIG, settings, "EMP001", opener=Raw(b"<html>login</html>")))
            path = hd.fetch_photo(CONFIG, settings, "EMP001", opener=Raw(b"\xff\xd8\xff\xe0jpeg"))
            self.assertEqual(path, hd.cached_photo(settings, "EMP001"))
            self.assertEqual(path.read_bytes(), b"\xff\xd8\xff\xe0jpeg")
            self.assertNotIn("EMP001", path.name)  # Codes never become file names directly.
            with patch.object(hd.hrm_sync, "employee_photo", return_value=None):
                self.assertIsNone(hd.fetch_photo(CONFIG, settings, "EMP001"))  # Removed in HRM.
            self.assertIsNone(hd.cached_photo(settings, "EMP001"))


class CacheAndMergeTests(unittest.TestCase):
    def test_sync_writes_a_cache_that_load_reads_back(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / hd.CACHE_NAME
            http = FakeHTTP(page([api_row("EMP002", "Nget", "Tim"),
                                  api_row("EMP009", "Old", "Staff", status="INACTIVE", terminated=True)], 0, 1, 2))
            directory = hd.sync(CONFIG, path, opener=http, now=1000.0)
            self.assertEqual([row["employee_id"] for row in directory.employees], ["EMP002"])
            again = hd.load(path)
            self.assertEqual(again, directory)
            self.assertEqual(again.employees[0]["employee_name"], "Nget Tim")
            self.assertNotIn(KEY, path.read_text())

    def test_sync_without_configuration_says_what_to_set(self):
        with self.assertRaisesRegex(HRMError, "load employees from HRM"):
            hd.sync(HRMConfig("", ""), "unused.json")

    def test_a_broken_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / hd.CACHE_NAME
            path.write_text("{not json")
            self.assertFalse(hd.load(path).synced)
            path.write_text(json.dumps({"version": 99, "employees": [{"employee_id": "X"}]}))
            self.assertFalse(hd.load(path).synced)

    def test_merge_lists_every_hrm_employee_and_every_local_enrollment(self):
        directory = hd.Directory(tuple(hd.normalize(r) for r in (
            api_row("EMP002", "Nget", "Tim", department="IT"),
            api_row("EMP010", "Sok", "Dara", department="HR"))), 1.0)
        local = [{"name": "Tim Dev", "employee_id": "emp002", "employee_name": "Tim Dev", "samples": 3,
                  "active": True},
                 {"name": "Guest", "employee_id": "LOCAL1", "employee_name": "Guest", "samples": 1,
                  "active": True}]
        rows = {row["employee_id"]: row for row in hd.merge(local, directory)}
        self.assertEqual(set(rows), {"emp002", "LOCAL1", "EMP010"})
        self.assertTrue(rows["emp002"]["enrolled"] and rows["emp002"]["in_hrm"])
        self.assertEqual(rows["emp002"]["department"], "IT")
        self.assertEqual(rows["emp002"]["samples"], 3)
        self.assertFalse(rows["LOCAL1"]["in_hrm"])
        self.assertFalse(rows["EMP010"]["enrolled"])
        self.assertEqual(rows["EMP010"]["samples"], 0)
        self.assertEqual(rows["EMP010"]["name"], "")

    def test_enrolled_status_comes_from_hrm_and_falls_back_to_the_local_count(self):
        directory = hd.Directory(tuple(hd.normalize(r) for r in (
            api_row("EMP001", "A", "One", faceEnrolled=True),
            api_row("EMP002", "B", "Two", faceEnrolled=False),
            api_row("EMP003", "C", "Three"))), 1.0)   # A server or cache without the field.
        counts = {"emp002": 6, "emp003": 2}
        rows = {row["employee_id"]: row for row in hd.merge(
            [{"name": "Two", "employee_id": "EMP002", "employee_name": "Two", "samples": 6, "active": True},
             {"name": "Guest", "employee_id": "LOCAL1", "employee_name": "Guest", "samples": 1, "active": True}],
            directory, counts)}
        self.assertTrue(hd.is_enrolled(rows["EMP001"]))   # No local angles yet; HRM has verified ones.
        self.assertFalse(hd.is_enrolled(rows["EMP002"]))  # Local angles, but HRM says they don't qualify.
        self.assertTrue(hd.is_enrolled(rows["EMP003"]))
        self.assertIsNone(rows["LOCAL1"]["face_enrolled"])
        self.assertFalse(hd.is_enrolled(rows["LOCAL1"]))
        needs = hd.Filters(enrollment=hd.NEEDS).apply(list(rows.values()))
        self.assertEqual({row["employee_id"] for row in needs}, {"EMP002", "LOCAL1"})

    def test_filters_and_pages(self):
        directory = hd.Directory(tuple(hd.normalize(api_row(f"E{n:03}", f"P{n:03}", "X",
                                                             department="IT" if n % 2 else "HR",
                                                             status="ACTIVE" if n < 50 else "TERMINATED"))
                                       for n in range(60)), 1.0)
        rows = hd.merge([{"name": "P001 X", "employee_id": "E001", "employee_name": "P001 X", "samples": 2,
                          "active": True}], directory)
        self.assertEqual(len(hd.Filters(department="IT").apply(rows)), 30)
        self.assertEqual(len(hd.Filters(enrollment=hd.NEEDS).apply(rows)), 59)
        self.assertEqual([r["employee_id"] for r in hd.Filters(enrollment=hd.READY).apply(rows)], ["E001"])
        self.assertEqual(len(hd.Filters(search="p00").apply(rows)), 10)
        self.assertEqual(len(hd.Filters(search="it").apply(rows)), 30)  # department text is searchable
        self.assertEqual(hd.Filters(enrollment=hd.NOT_IN_HRM).apply(rows), [])
        shown, number, pages = hd.paginate(rows, 9, 25)
        self.assertEqual((len(shown), number, pages), (10, 2, 3))
        self.assertEqual(hd.paginate([], 3, 25), ([], 0, 1))
        self.assertEqual(hd.options(rows, "department"), ["HR", "IT"])


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.path = Path(self._root.name) / hd.CACHE_NAME
        hd.sync(CONFIG, self.path, opener=FakeHTTP(page(
            [api_row("EMP001", "A", "One"), api_row("OLD002", "B", "Two"), api_row("emp003", "C", "Three")], 0, 1, 3)),
            now=100.0)

    def _preview(self, rows, total=None):
        return hd.preview_override(CONFIG, self.path, opener=FakeHTTP(page(rows, 0, 1, total or len(rows))), now=200.0)

    def _codes(self):
        return {row["employee_id"] for row in hd.load(self.path).employees}

    def test_the_check_matches_by_employee_id_and_changes_nothing_yet(self):
        preview = self._preview([api_row("EMP001", "A", "Renamed"), api_row("EMP003", "C", "Three"),
                                 api_row("NEW004", "D", "Four")])
        self.assertEqual([row["employee_id"] for row in preview.removed], ["OLD002"])
        self.assertEqual([row["employee_id"] for row in preview.added], ["NEW004"])
        self.assertEqual(preview.matched, 2)    # emp003 and EMP003 are the same employee.
        self.assertEqual(preview.changed, 2)
        self.assertEqual(self._codes(), {"EMP001", "OLD002", "emp003"})
        self.assertEqual(hd.load(self.path).fetched_at, 100.0)

    def test_applying_deletes_the_unmatched_and_stores_the_server_list(self):
        preview = self._preview([api_row("EMP001", "A", "Renamed"), api_row("NEW004", "D", "Four")])
        stored = hd.apply_override(self.path, preview)
        self.assertEqual(self._codes(), {"EMP001", "NEW004"})
        self.assertEqual(stored.fetched_at, 200.0)
        self.assertEqual(next(r for r in stored.employees if r["employee_id"] == "EMP001")["last_name"], "Renamed")

    def test_an_empty_or_partial_answer_never_deletes_anyone(self):
        with self.assertRaisesRegex(HRMError, "no active employees"):
            self._preview([])
        with self.assertRaisesRegex(HRMError, "only 1 of 3"):
            self._preview([api_row("EMP001", "A", "One")], total=3)
        self.assertEqual(self._codes(), {"EMP001", "OLD002", "emp003"})

    def test_a_save_that_did_not_take_is_an_error(self):
        preview = self._preview([api_row("EMP001", "A", "One")])
        with patch.object(hd, "_atomic_json"), self.assertRaisesRegex(OSError, "still holds"):
            hd.apply_override(self.path, preview)

    def test_an_employee_who_left_is_not_a_match(self):
        preview = self._preview([api_row("EMP001", "A", "One"), api_row("EMP003", "C", "Three"),
                                 api_row("OLD002", "B", "Two", status="TERMINATED")])
        self.assertEqual([row["employee_id"] for row in preview.removed], ["OLD002"])


class OverrideScreenTests(unittest.TestCase):
    def setUp(self):
        from tests.test_dashboard import EmployeesScreenTests, _app
        self.application = _app()
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.screen = EmployeesScreenTests()._screen(self._root.name)
        self.addCleanup(self.screen.deleteLater)
        self.path = hd.cache_path(self.screen.settings)
        hd.sync(CONFIG, self.path, opener=FakeHTTP(page([api_row("EMP001", "A", "One"),
                                                         api_row("OLD002", "B", "Two")], 0, 1, 2)))

    def _override(self, rows, confirmed):
        real_check = hd.preview_override

        def check(config, path, progress=None):
            return real_check(config, path, opener=FakeHTTP(page(rows, 0, 1, len(rows))))
        with patch.object(self.screen, "_hrm_config", return_value=CONFIG), \
                patch.object(hd, "preview_override", side_effect=check), \
                patch.object(self.screen, "_confirm_override", return_value=confirmed) as confirm:
            self.assertTrue(self.screen.override_from_hrm())
            deadline = time.monotonic() + 5
            while self.screen._hrm_syncing and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.005)
        self.assertFalse(self.screen._hrm_syncing)
        self.assertTrue(self.screen.override_action.isEnabled())
        self.assertTrue(self.screen.hrm_sync_button.isEnabled())
        return confirm

    def _codes(self):
        return {row["employee_id"] for row in hd.load(self.path).employees}

    def test_saying_no_keeps_the_file(self):
        confirm = self._override([api_row("EMP001", "A", "One")], False)
        confirm.assert_called_once()
        self.assertEqual(self._codes(), {"EMP001", "OLD002"})

    def test_saying_yes_deletes_the_unmatched_and_refreshes_the_table(self):
        self._override([api_row("EMP001", "A", "One")], True)
        self.assertEqual(self._codes(), {"EMP001"})
        listed = {row["employee_id"] for row in self.screen.directory_rows if row.get("in_hrm")}
        self.assertEqual(listed, {"EMP001"})

    def test_nothing_to_delete_needs_no_confirmation(self):
        confirm = self._override([api_row("EMP001", "A", "One"), api_row("OLD002", "B", "Two"),
                                  api_row("NEW003", "C", "Three")], False)
        confirm.assert_not_called()
        self.assertEqual(self._codes(), {"EMP001", "OLD002", "NEW003"})

    def test_a_failed_check_leaves_the_file_and_says_why(self):
        with patch.object(self.screen, "_notify") as notify:
            self._override([], True)
        self.assertEqual(self._codes(), {"EMP001", "OLD002"})
        self.assertIn("no active employees", notify.call_args.args[0])


class ScreenTests(unittest.TestCase):
    def test_directory_shows_hrm_employees_with_filters_and_pages(self):
        from tests.test_dashboard import EmployeesScreenTests, _app
        from face_attendance.enrollment import write_employee, write_encodings
        application = _app()
        with tempfile.TemporaryDirectory() as root:
            helper = EmployeesScreenTests()
            screen = helper._screen(root)
            try:
                rows = [api_row(f"EMP{n:03}", f"Person{n:03}", "Test", department="IT" if n % 2 else "HR")
                        for n in range(1, 61)]
                hd.sync(CONFIG, hd.cache_path(screen.settings), opener=FakeHTTP(page(rows, 0, 1, 60)))
                write_encodings(screen.settings.encodings_path, {"Person001 Test": [np.ones(128)]})
                write_employee(screen.settings.employees_path, "Person001 Test", "EMP001")
                from face_attendance import angle_templates as at
                at._save(at.store_path(screen.settings), {"EMP001": {"left": {
                    "sha256": "x", "version": 1, "problem": "", "check": "matched",
                    "vector": at._vector_text(np.ones(128))}}})
                screen._hrm_directory = hd.load(hd.cache_path(screen.settings))
                screen.reload()
                self.assertEqual(len(screen.directory_rows), 60)
                self.assertEqual(screen.employee_table.rowCount(), 25)
                self.assertIn("Showing 1–25 of 60", screen.result_count.text())
                self.assertEqual(screen.page_label.text(), "Page 1 of 3")
                screen.next_page_button.click()
                self.assertEqual(screen.page_label.text(), "Page 2 of 3")
                screen.last_page_button.click()
                self.assertEqual(screen.employee_table.rowCount(), 10)
                screen.department_filter.setCurrentIndex(screen.department_filter.findData("IT"))
                self.assertEqual(len(screen.filtered_rows), 30)
                self.assertEqual(screen.page, 0)
                screen.status_filter.setCurrentIndex(1)  # Ready for attendance
                self.assertEqual([row["employee_id"] for row in screen._page_rows], ["EMP001"])
                self.assertIn("IT", screen.employee_table.item(0, 1).text())
                screen._clear_filters()
                self.assertEqual(len(screen.filtered_rows), 60)
                # An HRM-only row opens Add employee with its name and ID filled in.
                index = next(i for i, row in enumerate(screen._page_rows) if row["employee_id"] == "EMP002")
                screen.employee_table.selectRow(index)
                # Before angles are in use, enrolling still means local samples.
                with patch.object(screen, "_angle_counts", return_value=None):
                    screen._activate_selected()
                self.assertEqual(screen.id_edit.text(), "EMP002")
                self.assertTrue(screen.id_edit.isReadOnly())
                self.assertEqual(screen.name_edit.text(), "Person002 Test")
                screen.editor.reject()
                application.processEvents()
            finally:
                screen.close()


if __name__ == "__main__":
    unittest.main()
