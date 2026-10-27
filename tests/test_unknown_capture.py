"""Unknown observations remain separate, per-face, and never mark attendance."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from face_attendance.capture import face_capture
from face_attendance.unknown_capture import save_unknown_capture
from face_attendance.dashboard.telegram import TelegramService


class UnknownCaptureTests(unittest.TestCase):
    def test_two_faces_have_separate_photos_and_records(self):
        frame = np.zeros((100, 200, 3), dtype=np.uint8)
        frame[:, 100:] = 255
        boxes = [(20, 60, 80, 20), (20, 180, 80, 140)]
        with tempfile.TemporaryDirectory() as root:
            records = []
            for track_id, box in enumerate(boxes):
                crop, source = face_capture(frame, box, [boxes[1-track_id]])
                info = save_unknown_capture(root, crop, source, track_id, 3.2, 1700000000)
                photo = cv2.imread(info['snapshot'])
                self.assertLess(photo.shape[1], frame.shape[1])
                self.assertAlmostEqual(float(photo.mean()), track_id * 255, delta=1)
                record = json.loads(Path(info['snapshot']).with_name('record.json').read_text())
                self.assertFalse(record['identity_verified'])
                self.assertFalse(record['liveness_verified'])
                records.append(info['event_id'])
            self.assertNotEqual(*records)
            self.assertEqual(len(list((Path(root) / 'unknown').iterdir())), 2)

    def test_failed_write_leaves_no_partial_record(self):
        with tempfile.TemporaryDirectory() as root, patch('cv2.imwrite', return_value=False):
            with self.assertRaises(OSError):
                save_unknown_capture(root, np.zeros((10, 10, 3), np.uint8),
                                     (0, 10, 10, 0), 1, 3, 1700000000)
            self.assertEqual(list((Path(root) / 'unknown').iterdir()), [])

    def test_telegram_does_not_suppress_second_person(self):
        with patch.object(TelegramService, '_start_worker'), patch(
                'face_attendance.dashboard.telegram._HAS_REQUESTS', True):
            service = TelegramService('token', 'chat')
            service.send_unknown_alert(1, 3, event_id='a')
            service.send_unknown_alert(2, 3, event_id='b')
            service.send_unknown_alert(1, 4, event_id='a')
            self.assertEqual(service._queue.qsize(), 2)

    def test_disabled_telegram_does_not_enqueue(self):
        service = TelegramService(notify_unknown=False)
        service.send_unknown_alert(1, 3)
        self.assertTrue(service._queue.empty())
