"""Enrolled Employees lists active HRM employees and filters them by employee type."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from face_attendance import hrm_directory as hd
from face_attendance.models import Employee
from tests.test_dashboard import EmployeesScreenTests, _app


def hrm_row(code, kind="HR", status="ACTIVE"):
    return hd.normalize({"id": code, "employeeCode": code, "firstName": code, "status": status, "employeeType": kind})


def enrolled(code):
    return {"name": code, "employee_id": code, "employee_name": code, "samples": 2, "active": True}


DIRECTORY = hd.Directory((hrm_row("D1", "DRIVER"), hrm_row("K1", "KONKLA"), hrm_row("H1", "HR")), 1.0)


class DirectoryTests(unittest.TestCase):
    def test_the_type_comes_from_hrm_and_a_missing_one_is_empty(self):
        self.assertEqual(hrm_row("D1", "driver")["employee_type"], "DRIVER")
        self.assertEqual(hrm_row("D1", None)["employee_type"], "")

    def test_filtering_by_type_includes_enrolled_people(self):
        rows = hd.merge([enrolled("D1")], DIRECTORY)
        self.assertEqual([r["employee_id"] for r in hd.Filters(employee_type="DRIVER").apply(rows)], ["D1"])
        self.assertEqual(len(hd.Filters().apply(rows)), 3)
        self.assertTrue(hd.Filters(employee_type="DRIVER").active)
        self.assertEqual(hd.options(rows, "employee_type"), ["DRIVER", "HR", "KONKLA"])

    def test_someone_hrm_stopped_listing_is_not_ready_but_can_be_found(self):
        rows = hd.merge([enrolled("GONE"), enrolled("D1")], DIRECTORY)
        by_code = {row["employee_id"]: row for row in rows}
        self.assertTrue(by_code["GONE"]["hrm_blocked"])
        self.assertFalse(by_code["D1"]["hrm_blocked"])
        self.assertEqual([r["employee_id"] for r in hd.Filters(enrollment=hd.READY).apply(rows)], ["D1"])
        self.assertEqual([r["employee_id"] for r in hd.Filters(enrollment=hd.NOT_IN_HRM).apply(rows)], ["GONE"])
        self.assertIn("GONE", [r["employee_id"] for r in hd.Filters(enrollment=hd.LOCAL_INACTIVE).apply(rows)])
        # The Live Monitor agrees: they can't record attendance.
        self.assertFalse(hd.resolver(DIRECTORY)(Employee("GONE", "x")).active)

    def test_without_a_directory_nobody_is_blocked(self):
        rows = hd.merge([enrolled("E1")], hd.Directory())
        self.assertFalse(rows[0]["hrm_blocked"])
        self.assertEqual(len(hd.Filters(enrollment=hd.READY).apply(rows)), 1)

    def test_an_older_cache_keeps_blocking_leavers_and_asks_for_a_sync(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / hd.CACHE_NAME
            path.write_text(json.dumps({"version": hd.OLD_CACHE_VERSION, "fetched_at": 5.0, "source": "x", "employees": [
                {"employee_id": "A1", "employee_name": "A", "hrm_status": "ACTIVE"},
                {"employee_id": "T1", "employee_name": "T", "hrm_status": "TERMINATED"}]}))
            directory = hd.load(path)
        self.assertTrue(directory.outdated)
        self.assertEqual([row["employee_id"] for row in directory.employees], ["A1"])
        self.assertFalse(hd.resolver(directory)(Employee("T1", "x")).active)

    def test_an_unknown_cache_version_is_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / hd.CACHE_NAME
            path.write_text(json.dumps({"version": 99, "fetched_at": 5.0, "employees": [{"employee_id": "A1"}]}))
            self.assertFalse(hd.load(path).synced)


class ScreenTests(unittest.TestCase):
    def setUp(self):
        _app()
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.screen = EmployeesScreenTests()._screen(self._root.name)
        self.addCleanup(self.screen.deleteLater)
        self.screen._hrm_directory = DIRECTORY
        self.screen.reload()

    def _choices(self, combo):
        return [combo.itemText(index) for index in range(combo.count())]

    def test_the_status_filter_is_gone_and_type_takes_its_place(self):
        self.assertFalse(hasattr(self.screen, "hrm_status_filter"))
        self.assertEqual(self._choices(self.screen.employee_type_filter),
                         ["All employee types", "DRIVER", "HR", "KONKLA"])
        self.assertIn("Not in HRM", self._choices(self.screen.status_filter))

    def test_choosing_a_type_narrows_the_list_and_clear_resets_it(self):
        combo = self.screen.employee_type_filter
        total = len(self.screen.filtered_rows)
        combo.setCurrentIndex(combo.findData("KONKLA"))
        self.assertEqual([row["employee_id"] for row in self.screen.filtered_rows], ["K1"])
        self.screen._clear_filters()
        self.assertEqual(combo.currentIndex(), 0)
        self.assertEqual(len(self.screen.filtered_rows), total)

    def test_an_outdated_cache_syncs_straight_away(self):
        self.screen._hrm_directory = hd.Directory(DIRECTORY.employees, 9e12, "x", outdated=True)
        with patch.object(self.screen, "_hrm_config") as config, patch.object(self.screen, "sync_hrm") as sync:
            config.return_value.configured = True
            self.screen._auto_sync_hrm()
        sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
