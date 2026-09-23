import unittest

import cv2
import numpy as np

from face_attendance.liveness import (
    LivenessChecker, MAX_SAMPLE_GAP, PASS_VALID_SEC, _measure,
    _ear,
    color_score,
    focus_variance,
    moire_score,
    texture_score,
)


def _real_face_roi():
    """Simulate a realistic face ROI with skin-like colour and texture."""
    rng = np.random.default_rng(42)
    roi = np.zeros((80, 60, 3), dtype=np.uint8)
    for y in range(roi.shape[0]):
        for x in range(roi.shape[1]):
            roi[y, x] = (
                140 + int(10 * np.sin(y / 4)) + rng.integers(-8, 9),
                160 + int(8 * np.cos(x / 3)),
                200 + int(5 * np.sin((x + y) / 5)),
            )
    return roi


def _uniform_roi():
    """Completely flat colour — e.g. a solid-colour card."""
    return np.full((80, 60, 3), (150, 165, 210), dtype=np.uint8)


OPEN_EYE = [(0, 0), (10, 6), (20, 6), (30, 0), (20, -6), (10, -6)]
CLOSED_EYE = [(0, 0), (10, 1), (20, 1), (30, 0), (20, -1), (10, -1)]


class TextureScoreTests(unittest.TestCase):
    def test_textured_image_scores_high(self):
        roi = _real_face_roi()
        self.assertGreaterEqual(texture_score(roi), 0.45)

    def test_completely_uniform_scores_low(self):
        self.assertLess(texture_score(_uniform_roi()), 0.45)

    def test_none_input(self):
        self.assertEqual(texture_score(None), 0.0)

    def test_tiny_input(self):
        self.assertEqual(texture_score(np.zeros((2, 2, 3), dtype=np.uint8)), 0.0)


class ColorScoreTests(unittest.TestCase):
    def test_skin_like_colours_score_high(self):
        roi = _real_face_roi()
        self.assertGreaterEqual(color_score(roi), 0.35)

    def test_non_skin_colours_score_low(self):
        roi = np.full((80, 60, 3), (255, 0, 0), dtype=np.uint8)
        self.assertLess(color_score(roi), 0.35)

    def test_none_input(self):
        self.assertEqual(color_score(None), 0.0)


class FocusVarianceTests(unittest.TestCase):
    def test_detailed_image_passes(self):
        rng = np.random.default_rng(42)
        roi = rng.integers(0, 255, (80, 60, 3), dtype=np.uint8)
        self.assertGreaterEqual(focus_variance(roi), 0.4)

    def test_uniformly_blurry_fails(self):
        roi = np.full((80, 60, 3), 128, dtype=np.uint8)
        self.assertLess(focus_variance(roi), 0.4)


class MoireScoreTests(unittest.TestCase):
    def test_natural_image_passes(self):
        roi = _real_face_roi()
        self.assertGreaterEqual(moire_score(roi), 0.4)

    def test_none_passes(self):
        self.assertEqual(moire_score(None), 1.0)


class EarTests(unittest.TestCase):
    def test_open_eye(self):
        self.assertGreater(_ear(OPEN_EYE), 0.3)

    def test_closed_eye(self):
        self.assertLess(_ear(CLOSED_EYE), 0.1)

    def test_wrong_length(self):
        self.assertIsNone(_ear([(0, 0)]))


def landmarks(eye=OPEN_EYE, yaw=0.0, mouth=.05, smile=False):
    # Eye centers at x=15 and x=75; mouth corners 40 pixels apart.
    top = [(25., 60.)] * 12
    bottom = [(25., 60.)] * 12
    top[6] = (65., 60.)
    if smile:
        top[0], top[6] = (20., 56.), (70., 56.)
    top[9] = (45., 60.)
    bottom[9] = (45., 60. + mouth * 40)
    return {"left_eye": eye, "right_eye": [(x + 60, y) for x, y in eye],
            "nose_bridge": [(45. + yaw * 60, 30.)],
            "top_lip": top, "bottom_lip": bottom}


def response_for(checker, track_id=1, eye=OPEN_EYE):
    """Simulated user blinks once when prompted."""
    state = checker._state.get(track_id, {})
    closed = (state.get("phase") == "ready" and state.get("closed_since") is None
              and state.get("open_since") is not None
              and state["last_sample"] - state["open_since"] >= .08)
    return landmarks(CLOSED_EYE if closed else eye)


def complete_challenge(checker, track_id=1, start=0):
    roi = _real_face_roi()
    for index in range(60):
        now = start + index * .1
        checker.update(track_id, response_for(checker, track_id), now)
        checker.update_texture(track_id, roi, now)
        if checker.is_live(track_id, now):
            return now
    raise AssertionError("Single blink did not complete")


class LivenessCheckerTests(unittest.TestCase):
    def setUp(self):
        self.checker = LivenessChecker()
        self.roi = _real_face_roi()

    def sample(self, at, pose=None, roi=None):
        self.checker.update(1, landmarks() if pose is None else pose, at)
        self.checker.update_texture(1, self.roi if roi is None else roi, at)
        return self.checker.is_live(1, at)

    def calibrate(self, eye=OPEN_EYE):
        for i in range(5):
            self.sample(i * .1, landmarks(eye))

    def test_one_blink_passes_without_any_head_actions(self):
        at = complete_challenge(self.checker)
        self.assertLess(at, 1.5)
        self.assertTrue(self.checker.is_live(1, at))
        self.assertEqual(self.checker._state[1]["method"], "blink")

    def test_smile_alone_passes_without_blink_or_return_to_neutral(self):
        self.calibrate()
        for i in range(5, 10):
            self.sample(i * .1, landmarks(smile=True))
        self.assertTrue(self.checker.is_live(1, .9))
        self.assertEqual(self.checker._state[1]["method"], "smile")

    def test_narrow_eyes_use_relative_blink_threshold(self):
        narrow = [(x, y * .45) for x, y in OPEN_EYE]
        self.assertLess(_ear(narrow), .21)
        self.calibrate(narrow)
        self.sample(.6, landmarks(CLOSED_EYE))
        self.assertTrue(self.sample(.75, landmarks(narrow)))

    def test_blink_route_does_not_require_lip_landmarks(self):
        for i in range(10):
            pose = landmarks(CLOSED_EYE if i in (6, 7) else OPEN_EYE)
            pose.pop("top_lip")
            pose.pop("bottom_lip")
            self.sample(i * .1, pose)
        self.assertTrue(self.checker.is_live(1, .9))

    def test_smile_works_with_five_samples_per_second(self):
        for i in range(7):
            self.sample(i * .2, landmarks(smile=i >= 3))
        self.assertTrue(self.checker.is_live(1, 6 * .2))

    def test_static_open_closed_and_smiling_photos_are_blocked(self):
        for pose in (landmarks(), landmarks(CLOSED_EYE), landmarks(smile=True)):
            self.checker.clear()
            for i in range(100):
                self.assertFalse(self.sample(i * .1, pose))

    def test_opening_mouth_or_one_frame_smile_does_not_pass(self):
        self.calibrate()
        self.sample(.6, landmarks(smile=True))
        self.sample(.7)
        for i in range(8, 20):
            self.assertFalse(self.sample(i * .1, landmarks(mouth=.4)))

    def test_eye_jitter_and_wink_do_not_count(self):
        self.calibrate()
        for i in range(5, 15):
            pose = landmarks([(x, y * .85) for x, y in OPEN_EYE])
            self.assertFalse(self.sample(i * .1, pose))
        pose = landmarks(CLOSED_EYE)
        pose["right_eye"] = landmarks()["right_eye"]
        self.sample(1.5, pose)
        self.assertFalse(self.sample(1.6))

    def test_prolonged_eye_closure_does_not_count(self):
        self.calibrate()
        for i in range(5, 17):
            self.sample(i * .1, landmarks(CLOSED_EYE))
        self.assertFalse(self.sample(1.8))

    def test_missing_frame_preserves_calibration_but_breaks_blink(self):
        self.calibrate()
        self.sample(.6, landmarks(CLOSED_EYE))
        self.sample(.7, {})
        self.assertFalse(self.sample(.8))
        self.assertEqual(self.checker.prompt(1), "Blink once or smile")
        self.sample(1., landmarks(CLOSED_EYE))
        self.assertTrue(self.sample(1.1))

    def test_missing_evidence_revokes_completed_pass(self):
        for bad in ({}, landmarks(eye=[]), landmarks(eye=[(0, 0)] * 6),
                    landmarks(eye=[(float("nan"), 0)] * 6)):
            self.checker.clear()
            now = complete_challenge(self.checker)
            self.sample(now + .1, bad)
            self.assertFalse(self.checker.is_live(1, now + .1))
        now = complete_challenge(self.checker, start=20)
        self.checker.update_texture(1, None, now)
        self.assertFalse(self.checker.is_live(1, now))

    def test_detection_gap_revokes_completed_pass(self):
        now = complete_challenge(self.checker)
        self.checker.pause(1)
        self.assertFalse(self.checker.is_live(1, now))

    def test_expiry_requires_new_expression(self):
        now = complete_challenge(self.checker)
        self.assertFalse(self.checker.is_live(1, now + MAX_SAMPLE_GAP + .01))
        now = complete_challenge(self.checker, start=20)
        for i in range(1, int(PASS_VALID_SEC * 10) + 2):
            self.sample(now + i * .1)
        self.assertFalse(self.checker.is_live(1, now + PASS_VALID_SEC + .1))

    def test_uniform_roi_and_texture_without_expression_do_not_pass(self):
        for i in range(60):
            self.assertFalse(self.sample(i * .1, response_for(self.checker), _uniform_roi()))
        self.checker.clear()
        for i in range(10):
            self.checker.update_texture(1, self.roi, i * .1)
        self.assertFalse(self.checker.is_live(1, 1))

    def test_repeated_frames_cannot_form_a_blink(self):
        self.calibrate()
        for _ in range(10):
            self.sample(.6, landmarks(CLOSED_EYE))
            self.assertFalse(self.sample(.6))

    def test_track_isolation_and_clear(self):
        now = complete_challenge(self.checker)
        self.assertFalse(self.checker.is_live(2, now))
        self.checker.clear()
        self.assertFalse(self.checker.is_live(1, now))

    def test_translation_zoom_roll_do_not_create_a_smile(self):
        original = landmarks()
        for angle in (0, .3, -.3):
            rotation = np.array([[np.cos(angle), -np.sin(angle)],
                                 [np.sin(angle), np.cos(angle)]])
            changed = {key: [tuple(rotation @ np.asarray(point) * 2 + [100, 30])
                             for point in points] for key, points in original.items()}
            np.testing.assert_allclose(_measure(changed)[1], _measure(original)[1], atol=1e-8)

    def test_expression_checker_alone_cannot_identify_replays(self):
        self.calibrate()
        self.sample(.6, landmarks(CLOSED_EYE))
        self.assertTrue(self.sample(.8))
