"""Enrollment editor rejects work from old camera/editor states."""
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from tests import test_dashboard as dashboard
from face_attendance.channels import LatestValue
from face_attendance.models import FramePacket


class EmployeeSetupTests(unittest.TestCase):
    def setUp(self):
        self.app = dashboard._app()
        self.directory = tempfile.TemporaryDirectory()
        self.screen = dashboard.EmployeesScreenTests()._screen(self.directory.name)
        self.frame = np.full((240, 320, 3), 128, np.uint8)
        self.release = threading.Event()

    def tearDown(self):
        self.release.set()
        self.wait_for(lambda: not self.screen.sample_runner.busy and not self.screen._probe_busy)
        self.screen.close()
        self.app.processEvents()
        self.directory.cleanup()

    def wait_for(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(.005)
        return predicate()

    def block_sample(self):
        started = threading.Event()
        def inspect(frame, quality):
            started.set()
            self.release.wait(3)
            return 'ok', '', ()
        self.screen.service.inspect = inspect
        self.assertTrue(self.screen._submit_sample(self.frame))
        self.assertTrue(started.wait(1))

    def test_save_uses_service_selected_at_submission(self):
        from face_attendance.dashboard.screens.employees import EnrollmentRunner
        from face_attendance.enrollment import EnrollmentOutcome
        calls, scheduled = [], []
        original = SimpleNamespace(enroll_many=lambda *args: (calls.append('original'),
                                    EnrollmentOutcome('enrolled'))[1])
        replacement = SimpleNamespace(enroll_many=lambda *args: calls.append('replacement'))
        runner = EnrollmentRunner(original)
        def deferred_thread(target, args, **kwargs):
            scheduled.append((target,args))
            return SimpleNamespace(start=lambda: None)
        with patch('face_attendance.dashboard.screens.employees.threading.Thread',side_effect=deferred_thread):
            runner.submit([self.frame],'Alice','A')
        runner.service = replacement
        target,args = scheduled[0]
        target(*args)
        self.assertEqual(calls,['original'])

    def test_clear_invalidates_inflight_sample_and_allows_next_sample(self):
        self.block_sample()
        self.screen._clear_samples()
        self.release.set()
        self.assertTrue(self.wait_for(lambda: not self.screen.sample_runner.busy))
        self.app.processEvents()
        self.assertEqual(self.screen.pending, [])
        self.assertFalse(self.screen.validating)
        self.assertTrue(self.screen._submit_sample(self.frame))
        self.assertTrue(self.wait_for(lambda: len(self.screen.pending) == 1))

    def test_closed_editor_does_not_receive_late_sample(self):
        self.screen._open_enrollment()
        self.block_sample()
        self.screen.editor.reject()
        self.assertFalse(self.screen.editor.isVisible())
        self.screen._open_enrollment({'name': 'other', 'employee_name': 'Other', 'employee_id': 'E2'})
        self.release.set()
        self.assertTrue(self.wait_for(lambda: not self.screen.sample_runner.busy))
        self.app.processEvents()
        self.assertEqual(self.screen.pending, [])

    def test_switching_capture_source_invalidates_sample(self):
        self.block_sample()
        self.screen._switch_segment(1)
        self.release.set()
        self.assertTrue(self.wait_for(lambda: not self.screen.sample_runner.busy))
        self.app.processEvents()
        self.assertEqual(self.screen.pending, [])

    def test_countdown_waits_full_seconds_and_clear_cancels(self):
        self.screen.preview_on = True
        self.screen.countdown_box.setValue(3)
        with patch.object(self.screen, '_grab_sample') as grab:
            self.screen._start_countdown()
            self.assertEqual(self.screen.countdown, 3)
            self.screen._tick_countdown()
            self.screen._tick_countdown()
            grab.assert_not_called()
            self.screen._tick_countdown()
            grab.assert_called_once()
            self.screen._start_countdown()
            self.screen._clear_samples()
            self.assertFalse(self.screen.countdown_timer.isActive())
            self.assertEqual(self.screen.countdown, 0)
        self.screen.preview_on = False

    def test_stale_camera_frame_cannot_be_captured(self):
        frames = LatestValue()
        frames.put(FramePacket(1, time.monotonic() - 60, time.time(), 1, self.frame))
        self.screen.preview_camera = SimpleNamespace(frames=frames)
        self.screen.preview_on = True
        try:
            self.assertIsNone(self.screen._latest_preview_frame())
            self.screen._grab_sample()
            self.assertFalse(self.screen.validating)
        finally:
            self.screen.preview_on = False
            self.screen.preview_camera = None

    def test_repeated_sample_is_not_collected_again(self):
        self.screen.service.inspect = lambda frame, quality: ('ok', '', ())
        self.screen._submit_sample(self.frame)
        self.assertTrue(self.wait_for(lambda: len(self.screen.pending) == 1))
        self.assertFalse(self.screen._submit_sample(self.frame.copy()))
        self.assertEqual(len(self.screen.pending), 1)
        self.screen._clear_samples()
        self.assertTrue(self.screen._submit_sample(self.frame))
        self.assertTrue(self.wait_for(lambda: len(self.screen.pending) == 1))

    def test_stale_probe_cannot_trigger_auto_capture(self):
        context = object()
        self.screen._probe_context = context
        self.screen._invalidate_checks()
        self.screen.preview_on = True
        self.screen.auto_check.setChecked(True)
        try:
            with patch.object(self.screen, '_submit_sample') as submit:
                self.screen.on_probe((context, ('ok', '', None, (), self.frame)))
                submit.assert_not_called()
        finally:
            self.screen.preview_on = False

    def test_delayed_probe_is_not_used_as_an_auto_capture(self):
        frames = LatestValue()
        frames.put(FramePacket(2, time.monotonic(), time.time(), 1, self.frame))
        self.screen.preview_camera = SimpleNamespace(frames=frames)
        self.screen.preview_on = True
        self.screen.auto_check.setChecked(True)
        context = (object(), time.monotonic() - 60, 1)
        self.screen._probe_context = context
        try:
            with patch.object(self.screen, '_submit_sample') as submit:
                self.screen.on_probe((context, ('ok', '', None, (), self.frame)))
                submit.assert_not_called()
        finally:
            self.screen.preview_on = False
            self.screen.preview_camera = None

    def test_probe_postprocessing_errors_release_worker_and_allow_retry(self):
        frames = LatestValue()
        self.screen.preview_camera = SimpleNamespace(frames=frames)
        self.screen.preview_on = True
        self.screen.quality_check.setChecked(True)
        try:
            for sequence, boxes, quality in (
                (1, [(1, 2, 3)], None),
                (2, [(1, 20, 20, 1)], RuntimeError('quality unavailable')),
            ):
                with self.subTest(sequence=sequence):
                    frames.put(FramePacket(sequence, time.monotonic(), time.time(), 1, self.frame))
                    with patch.object(self.screen.service.backend, 'face_locations', return_value=boxes), \
                         patch('face_attendance.enrollment.frame_quality', side_effect=quality):
                        self.screen._probe_now()
                        self.assertTrue(self.wait_for(lambda: not self.screen.probing))
                    self.assertFalse(self.screen._probe_busy)
                    self.assertIn('Face detection failed:', self.screen.coach.text())
            frames.put(FramePacket(3, time.monotonic(), time.time(), 1, self.frame))
            self.screen.quality_check.setChecked(False)
            self.screen._probe_now()
            self.assertTrue(self.wait_for(lambda: not self.screen.probing))
            self.assertIn('Face ready', self.screen.coach.text())
        finally:
            self.screen.preview_on = False
            self.screen.preview_camera = None

    def test_probe_snapshots_widget_options_on_ui_thread(self):
        frames = LatestValue()
        frames.put(FramePacket(1, time.monotonic(), time.time(), 1, self.frame))
        self.screen.preview_camera = SimpleNamespace(frames=frames)
        self.screen.preview_on = True
        calls = []
        original = self.screen.quality_check.isChecked
        self.screen.quality_check.isChecked = lambda: (calls.append(threading.get_ident()), original())[1]
        try:
            self.screen._probe_now()
            self.assertTrue(self.wait_for(lambda: not self.screen.probing))
            self.assertEqual(calls, [threading.get_ident()])
        finally:
            self.screen.preview_on = False
            self.screen.preview_camera = None
