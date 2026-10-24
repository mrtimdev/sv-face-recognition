"""Desktop duplicate-face review is asynchronous and never edits enrollments."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PyQt6.QtWidgets import QPushButton, QLabel

from tests import test_dashboard as dashboard
from face_attendance.enrollment import write_encodings


def match():
    return SimpleNamespace(label='Tim Dev2', employee_id='E2', employee_name='Tim Dev2',
                           matched_label='Tim Dev', matched_employee_id='E1',
                           matched_employee_name='Tim Dev', distance=.1)


class EmployeeDuplicateReviewTests(unittest.TestCase):
    def setUp(self):
        self.app = dashboard._app()
        self.directory = tempfile.TemporaryDirectory()
        self.screen = dashboard.EmployeesScreenTests()._screen(self.directory.name)
        self.releases = []
        self.assertTrue(self.wait_for(lambda: self.screen._duplicates_scan is None))

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.wait_for(lambda: self.screen._duplicates_scan is None)
        self.screen.close()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for(self, condition, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(.005)
        return condition()

    def seed_matches(self):
        vector = np.ones(128) / np.sqrt(128)
        write_encodings(self.screen.settings.encodings_path, {'Tim Dev': [vector], 'Tim Dev2': [vector]})
        self.screen._duplicates_cache = (match(),)
        self.screen._duplicates_cached_key = self.screen._duplicate_catalog_key()
        self.screen.reload()

    def test_named_badges_filter_and_counterpart_action_do_not_modify_records(self):
        self.seed_matches()
        paths = [Path(self.screen.settings.encodings_path), Path(self.screen.settings.employees_path)]
        before = [path.read_bytes() for path in paths]
        for index in range(2):
            status = self.screen.employee_table.cellWidget(index, 3)
            self.assertIn('Possible duplicate face with Tim Dev', status.toolTip())
            self.assertIn('Review match', [label.text() for label in status.findChildren(QLabel)])
        self.screen.status_filter.setCurrentIndex(3)
        self.assertEqual(sum(not self.screen.employee_table.isRowHidden(i) for i in range(2)), 2)
        index = next(i for i, row in enumerate(self.screen.rows) if row['name'] == 'Tim Dev2')
        button = self.screen.employee_table.cellWidget(index, 5).findChild(QPushButton)
        action = next(a for a in button.menu().actions() if a.text() == 'Review match: Tim Dev (E1)')
        action.trigger()
        self.assertEqual(self.screen._profile_row['name'], 'Tim Dev')
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_duplicate_enrollment_retains_samples_and_names_existing_owner(self):
        sample = np.zeros((120, 120, 3), np.uint8)
        self.screen.pending = [sample]
        self.screen.saving = True
        outcome = SimpleNamespace(ok=False, status='duplicate_face', message='duplicate', conflicts=(match(),))
        with patch.object(self.screen, 'reload') as reload, \
             patch.object(self.screen.engine, 'reload_catalog') as catalog, \
             patch.object(self.screen.editor_toast, 'show_message') as toast:
            self.screen.on_enrollment_finished(outcome)
            reload.assert_not_called()
            catalog.assert_not_called()
            text = toast.call_args.args[0]
            self.assertIn('Tim Dev (E1)', text)
            self.assertIn('Update photo', text)
            self.assertIn('No enrollment was saved', text)
        self.assertEqual(len(self.screen.pending), 1)
        self.assertIs(self.screen.pending[0], sample)
        self.assertTrue(self.screen.save_button.isEnabled())

    def test_scan_runs_off_ui_thread_and_same_catalog_uses_cache(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def scan():
            calls.append(threading.get_ident())
            started.set()
            release.wait(3)
            return []
        self.screen.service.duplicate_faces = scan
        self.screen._duplicates_cached_key = None
        self.screen.reload()
        self.assertTrue(started.wait(1))
        self.screen.reload()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], threading.get_ident())
        release.set()
        self.assertTrue(self.wait_for(lambda: self.screen._duplicates_scan is None))
        self.screen.reload()
        self.app.processEvents()
        self.assertEqual(len(calls), 1)

    def test_catalog_change_discards_inflight_result_and_scans_new_catalog(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def scan():
            calls.append(1)
            if len(calls) == 1:
                started.set()
                release.wait(3)
                return [match()]
            return []
        self.screen.service.duplicate_faces = scan
        self.screen._duplicates_cached_key = None
        self.screen.reload()
        self.assertTrue(started.wait(1))
        write_encodings(self.screen.settings.encodings_path, {'Replacement': [np.ones(128)]})
        self.screen.reload()
        release.set()
        self.assertTrue(self.wait_for(lambda: len(calls) == 2 and self.screen._duplicates_scan is None))
        self.assertEqual(self.screen._duplicates_by_label, {})
        self.assertEqual(self.screen._duplicates_cached_key, self.screen._duplicate_catalog_key())

    def test_mapping_change_invalidates_cached_review(self):
        self.seed_matches()
        Path(self.screen.settings.employees_path).write_text('{}\n', encoding='utf-8')
        self.screen.service.duplicate_faces = lambda: []
        self.screen.reload()
        self.assertEqual(self.screen._duplicates_by_label, {})
        self.assertTrue(self.wait_for(lambda: self.screen._duplicates_scan is None))
        self.assertEqual(self.screen._duplicates_cache, ())

    def test_settings_path_change_discards_old_worker_results(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def old_scan():
            started.set()
            release.wait(3)
            return [match()]
        self.screen.service.duplicate_faces = old_scan
        self.screen._duplicates_cached_key = None
        self.screen.reload()
        self.assertTrue(started.wait(1))
        with tempfile.TemporaryDirectory() as other:
            settings = dashboard.temp_settings(other)
            self.screen.on_settings_changed(settings)
            release.set()
            self.assertTrue(self.wait_for(lambda: self.screen._duplicates_scan is None))
            self.assertEqual(self.screen._duplicates_by_label, {})
            self.assertEqual(self.screen._duplicates_cached_key, self.screen._duplicate_catalog_key())

    def test_refresh_retries_failed_scan(self):
        self.screen.service.duplicate_faces = lambda: (_ for _ in ()).throw(ValueError('catalog unavailable'))
        self.screen._refresh_directory()
        self.assertTrue(self.wait_for(lambda: self.screen._duplicates_scan is None))
        self.assertIn('Refresh to retry', self.screen.duplicate_review.text())
        self.screen.service.duplicate_faces = lambda: []
        self.screen.reload_button.click()
        self.assertTrue(self.wait_for(lambda: self.screen._duplicates_scan is None))
        self.assertEqual(self.screen._duplicates_error, '')
        self.assertIn('No cross-employee', self.screen.duplicate_review.text())

    def test_constructor_uses_attendance_face_tolerance(self):
        self.assertEqual(self.screen.service.duplicate_distance, self.screen.settings.face_tolerance)

    def test_real_scan_marks_existing_duplicate_and_names_counterpart(self):
        from face_attendance.enrollment import write_employee
        vector = np.eye(1,128)[0]
        write_encodings(self.screen.settings.encodings_path, {'Tim Dev':[vector], 'Tim Dev 2':[vector]})
        write_employee(self.screen.settings.employees_path,'Tim Dev','EMP002')
        before = Path(self.screen.settings.encodings_path).read_bytes()
        self.screen._refresh_directory()
        self.assertTrue(self.wait_for(lambda: self.screen._duplicates_scan is None))
        self.assertEqual(set(self.screen._duplicates_by_label),{'Tim Dev','Tim Dev 2'})
        index = next(i for i,row in enumerate(self.screen.rows) if row['name'] == 'Tim Dev 2')
        tooltip = self.screen.employee_table.cellWidget(index,3).toolTip()
        self.assertIn('Tim Dev (EMP002)',tooltip)
        self.assertEqual(self.screen.stat_enrolled.value_label.text(),'0')
        self.assertIn('2 employees need face-match review',self.screen.stat_enrolled.hint_label.text())
        self.assertEqual(Path(self.screen.settings.encodings_path).read_bytes(),before)
