import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from face_attendance.anti_spoof import (
    AntiSpoofService, PresentationGuard, SAMPLE_MAX_AGE, face_patch,
)
from face_attendance.models import Detection
from tests import test_attendance, test_pipeline, test_tracking
from tests.test_attendance import verified


class GuardTests(unittest.TestCase):
    def test_one_good_image_and_rapid_duplicate_samples_cannot_pass(self):
        guard = PresentationGuard()
        for at in (0, 0, .01, .02):
            self.assertFalse(guard.update(1, .99, at))
        for at in (.4, .6, .8, 1.):
            self.assertFalse(guard.update(1, .99, at))
        self.assertTrue(guard.update(1, .99, 1.25))
        self.assertFalse(guard.passed(2, 1.25))

    def test_bad_or_missing_score_revokes_pass_immediately(self):
        for score in (None, .1, .79, .89, float('nan'), float('inf'), 1.1):
            with self.subTest(score=score):
                guard = PresentationGuard()
                for at in (1, 1.25, 1.5, 1.75, 2., 2.25):
                    guard.update(1, .99, at)
                self.assertTrue(guard.passed(1, 2.25))
                self.assertFalse(guard.update(1, score, 2.5))
                self.assertFalse(guard.passed(1, 2.5))
                self.assertFalse(guard.update(1, .99, 2.6))

    def test_expired_evidence_cannot_be_reused(self):
        guard = PresentationGuard()
        for at in (1, 1.25, 1.5, 1.75, 2., 2.25):
            guard.update(1, .99, at)
        self.assertTrue(guard.passed(1, 2.25))
        self.assertFalse(guard.passed(1, 2.25 + SAMPLE_MAX_AGE + .01))
        self.assertFalse(guard.update(1, .99, 4))

    def test_gap_clears_accumulation(self):
        guard = PresentationGuard()
        guard.update(1, .99, 1)
        guard.update(1, .99, 1.2)
        self.assertFalse(guard.update(1, .99, 3))


class ModelTests(unittest.TestCase):
    def test_upstream_live_print_and_screen_with_real_models_and_detector(self):
        from face_attendance.face_backend import OpenCVFaceBackend
        root = Path(__file__).parent / 'fixtures' / 'anti_spoof'
        detector, model = OpenCVFaceBackend(), AntiSpoofService()
        for name, expected in (('live', True), ('print', False), ('screen', False)):
            frame = cv2.imread(str(root / (name + '.jpg')))
            self.assertIsNotNone(frame, name)
            # Match the application's detection scale; inference uses original pixels.
            small = cv2.resize(frame, (round(frame.shape[1] * .25), round(frame.shape[0] * .25)))
            boxes = detector.face_locations(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
            self.assertEqual(len(boxes), 1, name)
            sy, sx = frame.shape[0] / small.shape[0], frame.shape[1] / small.shape[1]
            t, r, b, l = boxes[0]
            result = model.inspect(frame, (t * sy, r * sx, b * sy, l * sx))
            self.assertEqual(result['error'], '', name)
            self.assertEqual(len(result['models']), 2)
            from face_attendance.anti_spoof import live_sample
            self.assertEqual(live_sample(result['live_score']), expected, name)

    def test_bundled_models_load_and_run_on_existing_opencv(self):
        model = AntiSpoofService()
        self.assertEqual(model.error, '')
        frame = np.full((400, 400, 3), 127, np.uint8)
        score, error = model.evaluate(frame, (100, 240, 240, 100))
        self.assertEqual(error, '')
        self.assertTrue(0 <= score <= 1)

    def test_missing_or_corrupt_models_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = np.zeros((300, 300, 3), np.uint8)
            for corrupted in (False, True):
                if corrupted:
                    (Path(directory) / 'MiniFASNetV2.onnx').write_bytes(b'corrupt')
                model = AntiSpoofService(directory)
                self.assertTrue(model.error)
                self.assertIsNone(model.evaluate(frame, (50, 200, 200, 50))[0])

    def test_small_clipped_or_invalid_faces_are_blocked(self):
        model = AntiSpoofService()
        frame = np.zeros((400, 400, 3), np.uint8)
        for box in ((10, 40, 40, 10), (-10, 200, 200, 20), (20, 500, 200, 20),
                    (float('nan'), 200, 200, 20)):
            self.assertIsNone(model.evaluate(frame, box)[0])

    def test_crop_preserves_bgr(self):
        frame = np.full((400, 400, 3), (10, 30, 200), np.uint8)
        crop = face_patch(frame, (0, 100, 100, 0), 2.7)
        self.assertEqual(crop.shape, (80, 80, 3))
        np.testing.assert_array_equal(crop[0, 0], (10, 30, 200))

    def test_crop_keeps_full_context_at_top_left_edge(self):
        frame = np.random.default_rng(15).integers(0, 255, (400, 400, 3), dtype=np.uint8)
        crop = face_patch(frame, (0, 100, 100, 0), 2.7)
        # Center (50,50), half extent 135: [-85,185] shifted to [0,270].
        expected = cv2.resize(frame[:271, :271], (80, 80))
        np.testing.assert_array_equal(crop, expected)

    def test_crop_keeps_full_context_at_bottom_right_edge(self):
        frame = np.random.default_rng(15).integers(0, 255, (400, 400, 3), dtype=np.uint8)
        crop = face_patch(frame, (300, 400, 400, 300), 2.7)
        expected = cv2.resize(frame[129:400, 129:400], (80, 80))
        np.testing.assert_array_equal(crop, expected)

    def test_tall_webcam_face_uses_entire_available_height(self):
        frame = np.random.default_rng(15).integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        crop = face_patch(frame, (70, 900, 410, 660), 4.)
        # The 4x context is limited by height, then shifted down from a
        # face center near the top. It must keep all 720 rows, not 600.
        span = 240 * (719 / 340)
        expected = cv2.resize(frame[:, int(780-span/2):int(780+span/2)+1], (80, 80))
        np.testing.assert_array_equal(crop, expected)

    def test_closeup_without_surrounding_context_is_blocked(self):
        model = AntiSpoofService()
        result = model.inspect(np.zeros((400, 400, 3), np.uint8), (50, 350, 350, 50))
        self.assertIsNone(result['live_score'])
        self.assertIn('farther back', result['error'])

    def test_one_model_rejection_cannot_be_averaged_away(self):
        class Net:
            def __init__(self, logits):
                self.logits = logits
            def setInput(self, blob):
                self.blob = blob
            def forward(self):
                return np.array([self.logits])
        model = AntiSpoofService.__new__(AntiSpoofService)
        model.error = ''
        model.nets = [(Net([0, 20, 0]), 2.7), (Net([20, 0, 0]), 4.)]
        score, error = model.evaluate(np.zeros((400, 400, 3), np.uint8), (100, 200, 200, 100))
        self.assertEqual(error, '')
        self.assertLess(score, .01)
        model.nets[0][0].logits = [float('nan'), 0, 0]
        self.assertIsNone(model.evaluate(np.zeros((400, 400, 3), np.uint8), (100, 200, 200, 100))[0])


class RecognitionPADTests(unittest.TestCase):
    def test_per_model_scores_reach_detection_without_repeating_inference(self):
        from unittest.mock import Mock
        from face_attendance.models import RecognitionRequest
        fixture = test_pipeline.RecognitionTests()
        fixture.setUp()
        inspector = Mock()
        inspector.inspect.return_value = {"live_score": .2, "error": "",
                                         "models": [{"live": .2}, {"live": .97}]}
        fixture.service.anti_spoof = inspector
        result = fixture.service.process(RecognitionRequest(fixture.packet, ()))
        self.assertTrue(result.detections)
        self.assertEqual(result.detections[0].spoof_model_scores, (.2, .97))
        self.assertEqual(inspector.inspect.call_count, len(result.detections))
        inspector.evaluate.assert_not_called()

    def test_rejection_clears_completed_expression_and_presence_even_without_encoding(self):
        fixture = test_tracking.TrackingTests()
        fixture.setUp()
        fixture.confirm_two()
        fixture.verify_liveness()
        track = fixture.tracker.tracks[1]
        track.verified_presence = 5
        fixture.detect(20, 1.6, [Detection(fixture.box_a, encoded=False, hint_id=1, spoof_score=.02)])
        self.assertFalse(track.liveness_ok)
        self.assertFalse(fixture.tracker.liveness.active(1))
        self.assertEqual(track.verified_presence, 0)
        self.assertIsNone(track.evidence_packet)

    def test_pad_runs_even_when_encoding_is_reused(self):
        from face_attendance.models import RecognitionRequest, TrackHint
        fixture = test_pipeline.RecognitionTests()
        fixture.setUp()
        calls = []
        class PAD:
            def evaluate(self, frame, box):
                calls.append(box)
                return .02, ''
        fixture.service.anti_spoof = PAD()
        hint = TrackHint(1, (40, 200, 200, 40), 'A', 9.8, 94, True)
        result = fixture.service.process(RecognitionRequest(fixture.packet, (hint,)))
        self.assertFalse(result.detections[0].encoded)
        self.assertEqual(result.detections[0].spoof_score, .02)
        self.assertEqual(len(calls), 1)

    def test_photo_replacing_live_face_revokes_pass(self):
        fixture = test_tracking.TrackingTests()
        fixture.setUp()
        for sequence, now in enumerate(np.arange(1, 4.25, .25)):
            fixture.detect(sequence * 6 + 1, now,
                           [Detection(fixture.box_a, fixture.a, .3, True, spoof_score=.99)])
        track = fixture.tracker.tracks[1]
        self.assertTrue(track.spoof_ok)
        fixture.detect(100, 4.3, [Detection(fixture.box_a, encoded=False, hint_id=1, spoof_score=.01)])
        self.assertFalse(track.spoof_ok)
        self.assertEqual(track.verified_presence, 0)
        self.assertIsNone(track.evidence_packet)
        self.assertIn('Face not verified', track.spoof_prompt)

    def test_identity_change_and_missing_detection_clear_pad(self):
        for reason in ('identity', 'missing', 'disconnect', 'overlap'):
            fixture = test_tracking.TrackingTests()
            fixture.setUp()
            for sequence, now in enumerate(np.arange(1, 4.25, .25)):
                fixture.detect(sequence * 6 + 1, now,
                    [Detection(fixture.box_a, fixture.a, .3, True, spoof_score=.99),
                     Detection(fixture.box_b, fixture.b, .3, True, spoof_score=.99)])
            self.assertTrue(fixture.tracker.tracks[1].spoof_ok)
            if reason == 'identity':
                fixture.detect(100, 4.3, [Detection(fixture.box_a, fixture.b, .3, True, spoof_score=.99)])
            elif reason == 'missing':
                fixture.detect(100, 4.3, [])
            elif reason == 'disconnect':
                fixture.tracker.suspend()
            else:
                fixture.tracker.tracks[2].bounding_box = fixture.box_a
                fixture.tracker._mark_overlaps()
            self.assertFalse(fixture.tracker.tracks[1].spoof_ok, reason)

    def test_blink_cannot_override_pad_when_writing_logs(self):
        fixture = test_attendance.AttendanceTests()
        fixture.setUp()
        track = verified()
        track.spoof_ok = False
        fixture.service.observe({1: track}, 10, 1000)
        self.assertEqual(fixture.service.update({1: track}, fixture.packet, 10, True), [])
        self.assertEqual(fixture.worker.events, [])
        self.assertEqual(fixture.worker.jobs, [])
