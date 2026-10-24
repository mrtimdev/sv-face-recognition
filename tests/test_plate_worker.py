"""Fresh preview and cancellation checks without camera hardware or OCR models."""
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import numpy as np

from face_attendance.config import Config
from license_plate.core import Plate
from license_plate.screen import PlateWorker


class PlateWorkerFreshnessTests(unittest.TestCase):
    def make_worker(self, shared):
        return PlateWorker(Config(), '.', .8, 1, 30, 0,
                           shared=shared, recognizer_factory=object)

    def test_preview_advances_while_inference_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        frame = np.zeros((10, 20, 3), dtype=np.uint8)
        current = [NS(sequence=1, generation=1, captured_at=time.monotonic(), frame=frame)]
        worker = self.make_worker(lambda: current[0])

        def read(*args):
            entered.set()
            release.wait(2)
            return []

        with patch('license_plate.screen.read_plates', side_effect=read):
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                current[0] = NS(sequence=2, generation=1, captured_at=time.monotonic(),
                                frame=np.ones_like(frame))
                self.assertIs(worker.preview_packet(), current[0])
                self.assertIs(worker.latest, current[0].frame)
                current[0].captured_at -= 100
                self.assertIsNone(worker.preview_packet())
            finally:
                worker.requestInterruption()
                release.set()
                self.assertTrue(worker.wait(3000))
        self.assertIsNone(worker.latest)

    def test_stale_or_reconnected_inference_cannot_save(self):
        for reconnect in (False, True):
            with self.subTest(reconnect=reconnect):
                frame = np.zeros((10, 20, 3), dtype=np.uint8)
                packet = NS(sequence=1, generation=1, captured_at=time.monotonic(), frame=frame)
                current = [packet]
                worker = self.make_worker(lambda: current[0])
                worker.automatic.set()
                calls = [0]

                def read(*args):
                    calls[0] += 1
                    if calls[0] > 1:
                        worker.requestInterruption()
                        return []
                    if reconnect:
                        current[0] = NS(sequence=2, generation=2,
                                        captured_at=time.monotonic(), frame=frame)
                    else:
                        packet.captured_at -= 100
                        # Stop after the stale result was rejected, avoiding a stale wait loop.
                        threading.Timer(.05, worker.requestInterruption).start()
                    return [Plate('ABC', .95, (0, 0, 10, 5))]

                with patch('license_plate.screen.read_plates', side_effect=read), \
                        patch('license_plate.screen.save_capture') as save:
                    worker.start()
                    try:
                        self.assertTrue(worker.wait(3000))
                        save.assert_not_called()
                        self.assertEqual(worker.policy.counts, {})
                    finally:
                        worker.requestInterruption()
                        worker.wait(3000)
