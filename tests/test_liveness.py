import unittest

import cv2
import numpy as np

from face_attendance.liveness import (
    LivenessChecker,
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


OPEN_EYE = [(0, 0), (1, 1), (2, 1), (3, 0), (2, -1), (1, -1)]
CLOSED_EYE = [(0, 0), (1, 0.05), (2, 0.05), (3, 0), (2, -0.05), (1, -0.05)]


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
        self.assertEqual(_ear([(0, 0)]), 1.0)


class LivenessCheckerTests(unittest.TestCase):
    def setUp(self):
        self.checker = LivenessChecker()

    def test_initially_not_live(self):
        self.assertFalse(self.checker.is_live(1))

    def test_blink_plus_texture_and_colour_passes(self):
        for _ in range(5):
            self.checker.update(1, {"left_eye": OPEN_EYE, "right_eye": OPEN_EYE})
        self.checker.update(1, {"left_eye": CLOSED_EYE, "right_eye": CLOSED_EYE})
        for _ in range(5):
            self.checker.update(1, {"left_eye": OPEN_EYE, "right_eye": OPEN_EYE})

        roi = _real_face_roi()
        for _ in range(5):
            self.checker.update_texture(1, roi)

        self.assertTrue(self.checker.is_live(1))
        self.assertTrue(self.checker.passed(1))

    def test_no_blink_blocks_with_landmarks(self):
        """Static photo shows landmarks but never blinks — must be rejected."""
        for _ in range(15):
            self.checker.update(1, {"left_eye": OPEN_EYE, "right_eye": OPEN_EYE})

        roi = _real_face_roi()
        for _ in range(5):
            self.checker.update_texture(1, roi)

        self.assertFalse(self.checker.is_live(1))

    def test_uniform_texture_blocks_with_blink(self):
        """Uniform card held up with eye holes — blinks but no texture."""
        for _ in range(5):
            self.checker.update(1, {"left_eye": OPEN_EYE, "right_eye": OPEN_EYE})
        self.checker.update(1, {"left_eye": CLOSED_EYE, "right_eye": CLOSED_EYE})
        for _ in range(5):
            self.checker.update(1, {"left_eye": OPEN_EYE, "right_eye": OPEN_EYE})

        for _ in range(5):
            self.checker.update_texture(1, _uniform_roi())

        self.assertFalse(self.checker.is_live(1))

    def test_no_landmarks_fallback_with_good_texture(self):
        """Backend without face_landmarks: texture + colour gate instead."""
        roi = _real_face_roi()
        for _ in range(5):
            self.checker.update_texture(1, roi)

        self.assertTrue(self.checker.is_live(1))

    def test_no_landmarks_fallback_blocks_uniform(self):
        for _ in range(5):
            self.checker.update_texture(1, _uniform_roi())

        self.assertFalse(self.checker.is_live(1))

    def test_passed_is_cached(self):
        self.checker._state[1]["passed"] = True
        self.assertTrue(self.checker.is_live(1))

    def test_reset_clears_state(self):
        self.checker._state[1]["passed"] = True
        self.checker.reset(1)
        self.assertFalse(self.checker.passed(1))

    def test_clear_removes_all(self):
        self.checker._state[1]["blinks"] = 5
        self.checker._state[2]["blinks"] = 3
        self.checker.clear()
        self.assertEqual(self.checker.blink_count(1), 0)
        self.assertEqual(self.checker.blink_count(2), 0)

    def test_motion_variance_tracked(self):
        for _ in range(10):
            self.checker.update_motion(1, 2.5)
        state = self.checker._state[1]
        self.assertEqual(len(state["motion_variances"]), 10)

    def test_skips_texture_after_passed(self):
        self.checker._state[1]["passed"] = True
        self.checker.update_texture(1, _real_face_roi())
        self.assertEqual(len(self.checker._state[1]["texture_scores"]), 0)
