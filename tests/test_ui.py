import unittest
from types import SimpleNamespace

import numpy as np

from face_attendance.config import Config
from face_attendance.models import CaptureJob, SaveResult, State
from face_attendance.ui import Layout, UIRenderer
from tests.test_attendance import verified


class RendererTests(unittest.TestCase):
    def test_all_resolutions_states_and_animations_preserve_clean_input(self):
        for width, height in ((640, 480), (1280, 720), (1920, 1080)):
            with self.subTest(resolution=(width, height)):
                cfg = Config()
                renderer = UIRenderer(cfg)
                frame = np.full((height, width, 3), 67, np.uint8)
                track = verified(now=10)
                job = CaptureJob("event", 1, "E001", "Nget Tim", 1000, 3.1, frame.copy())
                result = SaveResult(job, "saved", 1000)
                attendance = SimpleNamespace(last_attendance=result, ready=True, storage_error="")
                for state in State:
                    track.state = state
                    track.verification_progress = .68
                    track.cooldown_remaining = 29
                    image = renderer.render(frame, {1: track}, 10.2, 1000.2, "CONNECTED", 30, 29.7, 125, attendance)
                    self.assertEqual(image.shape, (height, width + Layout(width, height).hud_width, 3))
                renderer.animations.capture(job, 10)
                renderer.animations.result("success", result, 10)
                renderer.render(frame, {1: track}, 10.1, 1000.1, "CONNECTED", 30, 30, 125, attendance)
                renderer.render(frame, {1: track}, 10.7, 1000.7, "RECONNECTING", 0, 30, 125, attendance)
                renderer.animations.result("error", SaveResult(job, "error", error="disk full"), 10.8)
                renderer.render(frame, {1: track}, 11.2, 1001.2, "CONNECTED", 30, 30, 125, attendance)
                self.assertTrue(np.all(frame == 67))

    def test_simultaneous_success_cards_fit_small_screen(self):
        renderer = UIRenderer(Config())
        frame = np.zeros((480, 640, 3), np.uint8)
        for index in range(4):
            job = CaptureJob(str(index), index, str(index), "Employee " * 20, 1000, 3, frame)
            renderer.animations.result("success", SaveResult(job, "saved", 1000), 10)
        attendance = SimpleNamespace(last_attendance=None, ready=True, storage_error="")
        renderer.render(frame, {}, 11, 1001, "CONNECTED", 30, 30, 4, attendance)
