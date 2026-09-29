import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np

from face_attendance.config import Config
from face_attendance.models import Detection, FramePacket, RecognitionRequest, State
from face_attendance.quality import FaceQuality, QualityResult
from face_attendance.recognition import RecognitionService
from tests.test_pipeline import FakeBackend
from tests.test_liveness import landmarks


def positioned_landmarks(box, yaw=0):
    t, r, b, l = box
    return {key: [(x + l + 20, y + t + 40) for x, y in pts]
            for key, pts in landmarks(yaw=yaw).items()}


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.quality = FaceQuality(Config())
        self.frame = np.random.default_rng(13).integers(70, 190, (240, 480, 3), dtype=np.uint8)
        self.box = (20, 180, 200, 20)

    def test_quality_separates_size_exposure_blur_and_clipping(self):
        self.assertTrue(self.quality.inspect(self.frame, self.box).ok)
        for frame, box, prompt in (
                (self.frame, (20, 60, 60, 20), 'closer'),
                (self.frame, (-1, 180, 200, 20), 'whole face'),
                (np.zeros_like(self.frame), self.box, 'lighting'),
                (np.full_like(self.frame, 255), self.box, 'lighting'),
                (cv2.GaussianBlur(self.frame, (31, 31), 8), self.box, 'blurry')):
            with self.subTest(prompt=prompt):
                result = self.quality.inspect(frame, box)
                self.assertFalse(result.ok)
                self.assertIn(prompt, result.prompt)

    def test_pose_and_missing_landmarks_block_without_claiming_spoof(self):
        basic = self.quality.inspect(self.frame, self.box)
        self.assertTrue(self.quality.landmarks(basic, positioned_landmarks(self.box), self.box).ok)
        side = self.quality.landmarks(basic, positioned_landmarks(self.box, yaw=.5), self.box)
        self.assertFalse(side.ok)
        self.assertIn('head upright', side.prompt)
        for lm in (None, {}, {'left_eye': [(float('nan'), 0)]}):
            self.assertFalse(self.quality.landmarks(basic, lm, self.box).ok)

    def test_quality_never_modifies_input(self):
        original = self.frame.copy()
        self.quality.inspect(self.frame, self.box)
        np.testing.assert_array_equal(original, self.frame)

    def service(self, mode='face'):
        backend = FakeBackend()
        backend.boxes = [(20, 180, 200, 20), (20, 440, 200, 280)]
        backend.face_landmarks = Mock(side_effect=lambda image, boxes, model: [positioned_landmarks(b) for b in boxes])
        pad = Mock()
        pad.inspect.return_value = {'live_score': .99, 'error': '', 'models': []}
        cfg = replace(Config(), detection_scale=1., attendance_mode=mode)
        service = RecognitionService(cfg, SimpleNamespace(employees=(), encodings=[]), backend, anti_spoof=pad)
        return service, backend, pad

    def test_one_bad_face_does_not_block_other_face_or_run_identity_and_pad(self):
        service, backend, pad = self.service()
        self.frame[20:200, 280:440] = 0
        result = service.process(RecognitionRequest(FramePacket(1, 1., 1., 1, self.frame), ()))
        good, bad = result.detections
        self.assertTrue(good.quality_ok)
        self.assertTrue(good.encoded)
        self.assertFalse(bad.quality_ok)
        self.assertFalse(bad.encoded)
        self.assertIsNone(bad.spoof_score)
        self.assertEqual(backend.encoded, 1)
        self.assertEqual(len(backend.face_landmarks.call_args.args[1]), 1)
        pad.inspect.assert_called_once()
        self.assertIs(pad.inspect.call_args.args[0], self.frame)

    def test_missing_mesh_fails_closed_even_in_face_only_mode(self):
        service, backend, pad = self.service()
        backend.face_landmarks.side_effect = RuntimeError('unavailable')
        result = service.process(RecognitionRequest(FramePacket(1, 1., 1., 1, self.frame), ()))
        self.assertTrue(all(not d.quality_ok and not d.encoded for d in result.detections))
        pad.inspect.assert_not_called()

    def test_quality_loss_revokes_only_affected_track_and_clears_evidence(self):
        from tests.test_tracking import TrackingTests
        fixture = TrackingTests()
        fixture.setUp()
        fixture.confirm_two()
        track = fixture.tracker.tracks[1]
        track.verified_presence, track.spoof_ok, track.liveness_ok = 2.9, True, True
        track.evidence_packet = fixture.packet(18, 1.5)
        fixture.detect(20, 1.6, [Detection(fixture.box_a, quality_ok=False, quality_prompt='Hold still'),
                               Detection(fixture.box_b, fixture.b, .3, True)])
        self.assertFalse(track.identity_valid)
        self.assertFalse(track.spoof_ok)
        self.assertFalse(track.liveness_ok)
        self.assertIsNone(track.evidence_packet)
        self.assertEqual(track.verified_presence, 0)
        self.assertEqual(track.state, State.DETECTING)
        self.assertTrue(fixture.tracker.tracks[2].identity_valid)

    def test_bad_quality_cannot_capture_or_emit_unknown_observation(self):
        from tests.test_attendance import verified, FakePersistence
        from face_attendance.attendance import AttendanceService
        worker = FakePersistence()
        attendance = AttendanceService(Config(), worker)
        worker.startup.put(({}, ''))
        track = verified()
        track.quality_ok = False
        attendance.poll({1: track}, 10, 1010)
        attendance.observe({1: track}, 10, 1010)
        self.assertEqual(attendance.update({1: track}, track.evidence_packet, 10, True), [])
        self.assertEqual(worker.events, [])
        self.assertEqual(worker.jobs, [])
