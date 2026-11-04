"""Live Monitor timing: no stretched frames, no fixed-sleep latency, paint-time conversion."""
import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from face_attendance.camera import fit_frame
from face_attendance.channels import LatestValue


class FitFrameTests(unittest.TestCase):
    def test_frames_keep_their_shape_when_shrunk(self):
        frame = np.zeros((720, 1280, 3), np.uint8)
        # A webcam that ignores a 1250x780 request must not be stretched to it.
        self.assertEqual(fit_frame(frame, 1250, 780).shape, (703, 1250, 3))
        self.assertEqual(fit_frame(np.zeros((1080, 1920, 3), np.uint8), 1280, 720).shape, (720, 1280, 3))

    def test_frames_that_fit_are_passed_through_untouched(self):
        frame = np.zeros((720, 1280, 3), np.uint8)
        self.assertIs(fit_frame(frame, 1280, 720), frame)
        self.assertIs(fit_frame(frame, 1920, 1080), frame)


class LatestValueWaitTests(unittest.TestCase):
    def test_wait_returns_as_soon_as_a_newer_value_lands(self):
        slot = LatestValue()
        version = slot.version
        threading.Timer(0.05, slot.put, args=("frame",)).start()
        started = time.monotonic()
        self.assertEqual(slot.wait_newer(version, 2.0), version + 1)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(slot.get(), "frame")

    def test_wait_gives_up_after_the_timeout(self):
        slot = LatestValue()
        started = time.monotonic()
        self.assertEqual(slot.wait_newer(slot.version, 0.05), 0)
        self.assertGreaterEqual(time.monotonic() - started, 0.04)

    def test_an_already_newer_value_does_not_wait(self):
        slot = LatestValue()
        version = slot.version
        slot.put(1)
        started = time.monotonic()
        slot.wait_newer(version, 5.0)
        self.assertLess(time.monotonic() - started, 0.5)


class VideoViewTests(unittest.TestCase):
    def test_hidden_view_keeps_the_frame_without_converting_it(self):
        from tests.test_dashboard import _app
        from face_attendance.dashboard.widgets.video_view import VideoView
        application = _app()
        view = VideoView()
        frame = np.full((90, 160, 3), 120, np.uint8)
        view.set_frame(frame)
        self.assertTrue(view.has_frame())
        self.assertIsNone(view._pixmap)
        view.resize(320, 240)
        view.show()
        view.repaint()
        application.processEvents()
        self.assertIsNotNone(view._pixmap)
        self.assertEqual((view._pixmap.width(), view._pixmap.height()), (160, 90))
        view.close()


if __name__ == "__main__":
    unittest.main()
