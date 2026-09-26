import unittest

import cv2
import numpy as np

from face_attendance.config import Config
from face_attendance.models import Detection, Employee, FramePacket, RecognitionResult
from face_attendance.tracking import FaceTracker


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()
        self.tracker = FaceTracker(self.cfg)
        self.frame = np.random.default_rng(7).integers(0, 255, (240, 480, 3), dtype=np.uint8)
        self.a, self.b = Employee("A", "Alice"), Employee("B", "Bob")
        self.box_a, self.box_b = (50, 150, 170, 30), (50, 420, 170, 300)

    def packet(self, sequence, now, generation=1, frame=None):
        return FramePacket(sequence, now, 1000 + now, generation, self.frame if frame is None else frame)

    def detect(self, seq, now, detections):
        packet = self.packet(seq, now)
        self.tracker.advance(packet)
        self.assertTrue(self.tracker.apply(RecognitionResult(packet, tuple(detections), 0.01), now + 0.01))

    def confirm_two(self):
        for i, now in enumerate((1, 1.2, 1.4)):
            detections = [Detection(self.box_a, self.a, 0.3, True), Detection(self.box_b, self.b, 0.3, True)]
            if i == 1:
                detections.reverse()
            self.detect(i * 6 + 1, now, detections)

    def test_order_changes_do_not_switch_employee_tracks(self):
        self.confirm_two()
        self.assertEqual(self.tracker.tracks[1].employee_id, "A")
        self.assertEqual(self.tracker.tracks[2].employee_id, "B")
        self.assertTrue(all(t.identity_valid for t in self.tracker.tracks.values()))

    def test_conflicting_identity_immediately_revokes_eligibility(self):
        self.confirm_two()
        self.detect(20, 1.6, [Detection(self.box_a, self.b, 0.3, True), Detection(self.box_b, self.b, 0.3, True)])
        track = self.tracker.tracks[1]
        self.assertFalse(track.identity_valid)
        self.assertEqual(track.verified_presence, 0)
        self.assertTrue(self.tracker.tracks[2].identity_valid)

    def test_unknown_or_missing_detection_resets_continuity(self):
        self.confirm_two()
        self.detect(20, 1.6, [Detection(self.box_a, None, 0.8, True)])
        self.assertFalse(self.tracker.tracks[1].identity_valid)
        self.assertFalse(self.tracker.tracks[2].visible)
        self.assertEqual(self.tracker.tracks[2].verified_presence, 0)

    def test_stale_and_out_of_order_results_are_ignored(self):
        self.confirm_two()
        packet = self.packet(3, 1.2)
        self.assertFalse(self.tracker.apply(RecognitionResult(packet, (), 0.01), 1.5))
        packet = self.packet(30, 1.0)
        self.assertFalse(self.tracker.apply(RecognitionResult(packet, (), 0.01), 3.0))
        self.assertEqual(len(self.tracker.tracks), 2)

    def test_reconnect_discards_tracks_and_old_generation_results(self):
        self.confirm_two()
        self.tracker.advance(self.packet(100, 2, generation=2))
        self.assertEqual(self.tracker.tracks, {})
        old = self.packet(99, 1.9)
        self.assertFalse(self.tracker.apply(RecognitionResult(old, (Detection(self.box_a, self.a, .3, True),), .01), 2))

    def test_optical_flow_follows_motion_between_detection_frames(self):
        self.detect(1, 1, [Detection(self.box_a, self.a, .3, True)])
        matrix = np.float32([[1, 0, 6], [0, 1, 3]])
        moved = cv2.warpAffine(self.frame, matrix, (480, 240))
        self.tracker.advance(self.packet(2, 1.033, frame=moved))
        track = self.tracker.tracks[1]
        self.assertTrue(track.flow_ok)
        self.assertAlmostEqual(track.bounding_box[3], self.box_a[3] + 6, delta=1)
        self.assertAlmostEqual(track.bounding_box[0], self.box_a[0] + 3, delta=1)

    def test_overlap_revokes_both_tracks(self):
        self.confirm_two()
        self.tracker.tracks[2].bounding_box = self.box_a
        self.tracker._mark_overlaps()
        self.assertTrue(all(t.ambiguous for t in self.tracker.tracks.values()))
        self.assertTrue(all(not t.identity_valid for t in self.tracker.tracks.values()))

    def test_brief_tracking_loss_freezes_verification_and_blocks_capture(self):
        self.confirm_two()
        for track in self.tracker.tracks.values():
            track.points = None
            track.verified_presence = 2.9
        self.tracker.advance(self.packet(15, 1.45, frame=np.zeros_like(self.frame)))
        for track in self.tracker.tracks.values():
            self.assertEqual(track.verified_presence, 2.9)
            self.assertFalse(track.flow_ok)
            self.assertIsNone(track.last_evidence_at)
            self.assertIsNone(track.evidence_packet)

    def test_persistent_tracking_loss_resets_presence_even_with_fresh_detections(self):
        self.confirm_two()
        track = self.tracker.tracks[1]
        track.verified_presence = 2.9
        for seq in range(20, 30):
            track.points = None
            self.detect(seq, 1.5 + (seq - 20) * .1, [Detection(
                self.box_a, self.a, .3, True, spoof_score=.99)])
            self.assertFalse(track.flow_ok)
        self.assertEqual(track.verified_presence, 0)
        self.assertIsNone(track.last_evidence_at)

    def test_unencoded_detection_cannot_inherit_unrelated_identity(self):
        self.confirm_two()
        self.detect(20, 1.6, [Detection(self.box_a, encoded=False, hint_id=999)])
        self.assertFalse(self.tracker.tracks[1].identity_valid)

    def verify_liveness(self):
        from tests.test_liveness import landmarks, complete_challenge
        for track in self.tracker.tracks.values():
            self.tracker.liveness.reset(track.track_id)
            at = complete_challenge(self.tracker.liveness, track.track_id, start=-5)
            while at < 1.4:
                at = min(1.4, at + .1)
                self.tracker.liveness.update(track.track_id, landmarks(), at)
            track.liveness_ok = self.tracker.liveness.is_live(track.track_id, 1.4)
            track.last_liveness_at = 1.4
            self.assertTrue(track.liveness_ok)

    def test_identity_loss_and_swap_clear_liveness(self):
        from tests.test_liveness import landmarks, _real_face_roi
        for employee in (self.b, None):
            with self.subTest(employee=employee):
                self.tracker.clear()
                self.tracker.next_id = 1
                self.confirm_two()
                self.verify_liveness()
                self.detect(20, 1.6, [Detection(self.box_a, employee, .3, True,
                                              landmarks=landmarks(), face_roi=_real_face_roi())])
                for track in self.tracker.tracks.values():
                    self.assertFalse(track.liveness_ok)
                    self.assertEqual(self.tracker.liveness.progress(track.track_id), 0)

    def test_suspend_and_overlap_clear_liveness(self):
        for reason in ("suspend", "overlap"):
            with self.subTest(reason=reason):
                self.tracker.clear()
                self.tracker.next_id = 1
                self.confirm_two()
                self.verify_liveness()
                if reason == "suspend":
                    self.tracker.suspend()
                elif reason == "overlap":
                    self.tracker.tracks[2].bounding_box = self.box_a
                    self.tracker._mark_overlaps()
                self.assertTrue(all(not t.liveness_ok for t in self.tracker.tracks.values()))
                self.assertTrue(all(not self.tracker.liveness.active(t.track_id)
                                    for t in self.tracker.tracks.values()))

    def test_liveness_updates_when_identity_encoding_is_skipped(self):
        from tests.test_liveness import landmarks, CLOSED_EYE, _real_face_roi
        self.confirm_two()
        self.verify_liveness()
        self.detect(20, 1.6, [Detection(self.box_a, encoded=False, hint_id=1,
                                      landmarks=landmarks(CLOSED_EYE), face_roi=_real_face_roi(), spoof_score=.99)])
        self.assertTrue(self.tracker.tracks[1].liveness_ok)
        self.assertEqual(self.tracker.tracks[1].last_liveness_at, 1.6)
        self.detect(22, 1.7, [Detection(self.box_a, encoded=False, hint_id=1)])
        self.assertFalse(self.tracker.tracks[1].liveness_ok)

    def test_expired_track_removes_liveness_state(self):
        self.confirm_two()
        self.verify_liveness()
        self.tracker.advance(self.packet(100, 10))
        self.assertEqual(self.tracker.tracks, {})
        self.assertFalse(self.tracker.liveness.active(1))
        self.assertFalse(self.tracker.liveness.active(2))

    def test_brief_optical_flow_loss_does_not_erase_expression_progress(self):
        from tests.test_liveness import response_for, _real_face_roi
        self.confirm_two()
        track = self.tracker.tracks[1]
        now = 1.5
        # Force transient loss before every fresh, identity-matched detection.
        for sequence in range(20, 250, 4):
            track.points = None
            self.detect(sequence, now, [Detection(self.box_a, self.a, .3, True,
                                                 landmarks=response_for(self.tracker.liveness),
                                                 face_roi=_real_face_roi(), spoof_score=.99)])
            if track.liveness_ok:
                break
            now += .1
        self.assertTrue(track.liveness_ok, "Flow resets prevented a single blink")

    def test_waiting_for_expression_does_not_force_frequent_encoding(self):
        self.confirm_two()
        track = self.tracker.tracks[1]
        track.verified_presence = 30
        track.liveness_ok = False
        self.assertTrue(self.tracker.hints()[0].stable)
        track.liveness_ok = True
        track.spoof_ok = True
        self.assertFalse(self.tracker.hints()[0].stable)
