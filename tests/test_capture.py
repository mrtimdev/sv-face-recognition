"""Person-specific crops, immutable evidence and companion-file persistence."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from face_attendance.capture import face_capture
from face_attendance.attendance import AttendanceService
from face_attendance.config import Config
from face_attendance.models import CaptureJob, FramePacket
from face_attendance.storage import SnapshotService, context_path, evidence_files
from tests.test_attendance import FakePersistence, verified


class FaceCropTests(unittest.TestCase):
    def test_padding_is_clipped_at_edges_and_avoids_neighbors(self):
        frame = np.zeros((200, 300, 3), np.uint8)
        frame[20:120, 10:90] = (0, 0, 255)
        frame[20:120, 95:175] = (0, 255, 0)
        crop, bounds = face_capture(frame, (20, 90, 120, 10), ((20, 175, 120, 95),))
        self.assertEqual(bounds[3], 0)
        self.assertLessEqual(bounds[1], 95)
        self.assertFalse(np.any(crop[:, :, 1]))
        self.assertTrue(np.any(crop[:, :, 2]))
        frame[:] = 0
        self.assertTrue(np.any(crop[:, :, 2]))

    def test_missing_invalid_and_overlapping_boxes_are_rejected(self):
        frame = np.zeros((200, 300, 3), np.uint8)
        for box in (None, (0, 20, 0, 0), (-1, 20, 30, 0), (0, float('nan'), 30, 0),
                    (0, 301, 40, 0)):
            with self.subTest(box=box), self.assertRaises(ValueError):
                face_capture(frame, box)
        with self.assertRaises(ValueError):
            face_capture(frame, (10, 100, 110, 10), ((10, 180, 110, 90),))

    def test_unrelated_partially_visible_face_does_not_block_capture(self):
        frame = np.full((200, 300, 3), 57, np.uint8)
        crop, _ = face_capture(frame, (20, 100, 120, 20), ((-20, 350, 110, 250),))
        self.assertTrue(np.all(crop == 57))


class SeparateAttendanceCaptureTests(unittest.TestCase):
    def setUp(self):
        self.worker = FakePersistence()
        self.service = AttendanceService(Config(), self.worker)
        self.worker.startup.put(({}, ""))
        self.service.poll({}, 10, 1000)
        self.packet = FramePacket(100, 10, 1000, 1, np.zeros((240, 320, 3), np.uint8))

    def test_two_people_get_distinct_crops_from_the_same_verified_frame(self):
        frame = np.zeros((240, 320, 3), np.uint8)
        frame[40:140, 20:100] = (0, 0, 255)
        frame[40:140, 200:280] = (0, 255, 0)
        a, b = verified(1, 'A'), verified(2, 'B')
        a.evidence_packet = b.evidence_packet = replace(self.packet, frame=frame)
        a.evidence_box, b.evidence_box = (40, 100, 140, 20), (40, 280, 140, 200)
        a.evidence_neighbors, b.evidence_neighbors = (b.evidence_box,), (a.evidence_box,)
        # Positions have moved since this frame was checked. They must not be used.
        a.bounding_box, b.bounding_box = b.evidence_box, a.evidence_box
        jobs = self.service.update({1: a, 2: b}, replace(self.packet, sequence=101), 10, True)
        self.assertEqual([job.employee_id for job in jobs], ['A', 'B'])
        self.assertTrue(np.any(jobs[0].frame[:, :, 2]))
        self.assertFalse(np.any(jobs[0].frame[:, :, 1]))
        self.assertTrue(np.any(jobs[1].frame[:, :, 1]))
        self.assertFalse(np.any(jobs[1].frame[:, :, 2]))
        self.assertEqual(jobs[0].source_sequence, 100)
        self.assertEqual(jobs[0].source_generation, 1)
        self.assertEqual(jobs[0].face_box, a.evidence_box)
        np.testing.assert_array_equal(jobs[0].context_frame, frame)
        frame[:] = 0
        self.assertTrue(np.any(jobs[0].context_frame))
        self.assertTrue(np.any(jobs[0].frame))

    def test_no_crop_when_face_evidence_is_missing_or_overlapping(self):
        for box, neighbors in ((None, ()), ((20, 100, 120, 20), ((20, 110, 120, 25),))):
            track = verified()
            track.evidence_box, track.evidence_neighbors = box, neighbors
            self.assertEqual(self.service.update({1: track}, self.packet, 10, True), [])


class EvidenceStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = SnapshotService(self.root / 'captures')
        self.job = CaptureJob('evt-1', 7, 'E1', 'Person', 1000, 3,
                              np.full((100, 80, 3), 57, np.uint8),
                              context_frame=np.full((240, 320, 3), 120, np.uint8),
                              face_box=(20, 100, 120, 20), crop_box=(0, 116, 140, 4),
                              source_sequence=18, source_generation=2)

    def test_both_images_and_binding_metadata_are_saved_and_deleted(self):
        snapshot = self.storage.save(self.job)
        self.assertEqual(cv2.imread(snapshot).shape, (100, 80, 3))
        self.assertEqual(cv2.imread(str(context_path(snapshot))).shape, (240, 320, 3))
        data = json.loads(context_path(snapshot).with_suffix('.json').read_text())
        self.assertEqual(data['employee_id'], 'E1')
        self.assertEqual(data['track_id'], 7)
        self.assertEqual(data['event_id'], 'evt-1')
        self.assertEqual(data['face_box'], [20, 100, 120, 20])
        self.assertEqual(data['source_sequence'], 18)
        self.assertEqual(len(list(self.storage.directory.glob('*.jpg'))), 1)
        self.assertEqual(self.storage.remove(snapshot), 2)
        self.assertTrue(all(not p.exists() for p in evidence_files(snapshot)))

    def test_failed_crop_write_removes_context_and_metadata(self):
        real_encode = cv2.imencode
        def encode(extension, frame, options):
            return (False, None) if frame.shape == self.job.frame.shape else real_encode(extension, frame, options)
        with patch('face_attendance.storage.cv2.imencode', side_effect=encode):
            with self.assertRaises(OSError):
                self.storage.save(self.job)
        self.assertFalse(any(p.is_file() for p in self.storage.directory.rglob('*')))

    def test_database_rollback_and_report_deletion_remove_companions(self):
        from face_attendance.repository import AttendanceRepository
        from face_attendance.report import delete_records
        db = self.root / 'attendance.db'
        repo = AttendanceRepository(db, clock=lambda: 2000)
        try:
            repo.connection.execute("""CREATE TRIGGER fail_outbox BEFORE INSERT ON attendance_outbox
                BEGIN SELECT RAISE(ABORT, 'failure'); END""")
            self.assertEqual(repo.record(self.job, self.storage).outcome, 'error')
            self.assertFalse(any(p.is_file() for p in self.storage.directory.rglob('*')))
            repo.connection.execute('DROP TRIGGER fail_outbox')
            saved = repo.record(self.job, self.storage)
            self.assertEqual(saved.outcome, 'saved')
            row_id = repo.connection.execute('SELECT id FROM attendance').fetchone()[0]
            self.assertEqual(delete_records(db, [row_id]), (1, 2))
            self.assertTrue(all(not path.exists() for path in evidence_files(saved.snapshot)))
        finally:
            repo.close()
