"""Deterministic reconnect checks without hardware or real retry delays."""
from dataclasses import replace
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from face_attendance.camera import CameraManager
from face_attendance.config import Config


class RetryStop:
    def __init__(self, attempts):
        self.attempts = attempts
        self.waits = []

    def is_set(self):
        return len(self.waits) >= self.attempts

    def wait(self, delay):
        self.waits.append(delay)
        return self.is_set()


class CameraReliabilityTests(unittest.TestCase):
    def test_open_stream_without_frames_uses_exponential_backoff(self):
        cap = NS(isOpened=lambda: True, read=lambda: (False, None), release=Mock())
        camera = CameraManager(replace(Config(), reconnect_sec=1), lambda: cap)
        camera.stop_event = RetryStop(7)
        camera._run()
        self.assertEqual(camera.stop_event.waits, [1, 2, 4, 8, 16, 30, 30])
        self.assertEqual(cap.release.call_count, 7)
        self.assertEqual(camera.status(0), 'STOPPED')

    def test_release_failure_does_not_end_reconnects(self):
        cap = NS(isOpened=lambda: False, release=Mock(side_effect=OSError('driver cleanup failed')))
        camera = CameraManager(replace(Config(), reconnect_sec=1), lambda: cap)
        camera.stop_event = RetryStop(3)
        with self.assertLogs(level='ERROR'):
            camera._run()
        self.assertEqual(cap.release.call_count, 3)
        self.assertEqual(camera.stop_event.waits, [1, 2, 4])
        self.assertEqual(camera.status(0), 'STOPPED')

    def test_configured_initial_retry_obeys_maximum_backoff(self):
        cap = NS(isOpened=lambda: False, release=Mock())
        camera = CameraManager(replace(Config(), reconnect_sec=120), lambda: cap)
        camera.stop_event = RetryStop(2)
        camera._run()
        self.assertEqual(camera.stop_event.waits, [30, 30])
