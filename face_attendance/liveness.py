"""Basic anti-spoofing via blink detection (Eye Aspect Ratio).

A printed photo or a static screen replay cannot blink, so requiring at
least one blink during the verification window blocks the simplest attacks.
This is not a substitute for depth sensing or IR-based liveness, but it
raises the bar significantly above zero.
"""
import math
from collections import defaultdict


EAR_BLINK_THRESHOLD = 0.21
EAR_OPEN_THRESHOLD = 0.26
MIN_BLINKS = 1


def _ear(eye_points):
    """Eye Aspect Ratio from six (x, y) landmarks."""
    if len(eye_points) != 6:
        return 1.0
    p1, p2, p3, p4, p5, p6 = eye_points
    vertical_a = math.dist(p2, p6)
    vertical_b = math.dist(p3, p5)
    horizontal = math.dist(p1, p4)
    if horizontal < 1e-6:
        return 1.0
    return (vertical_a + vertical_b) / (2.0 * horizontal)


class LivenessChecker:
    """Per-track blink counter using face_recognition landmarks."""

    def __init__(self, blink_threshold=EAR_BLINK_THRESHOLD,
                 open_threshold=EAR_OPEN_THRESHOLD,
                 min_blinks=MIN_BLINKS):
        self.blink_threshold = blink_threshold
        self.open_threshold = open_threshold
        self.min_blinks = min_blinks
        self._state = defaultdict(lambda: {"blinks": 0, "closed": False})

    def update(self, track_id, landmarks):
        """Feed one frame's landmarks for a track. Returns (blink_count, passed)."""
        left_eye = landmarks.get("left_eye", [])
        right_eye = landmarks.get("right_eye", [])
        if not left_eye or not right_eye:
            return self._result(track_id)
        ear = (_ear(left_eye) + _ear(right_eye)) / 2.0
        state = self._state[track_id]
        if ear < self.blink_threshold:
            state["closed"] = True
        elif ear > self.open_threshold and state["closed"]:
            state["closed"] = False
            state["blinks"] += 1
        return self._result(track_id)

    def _result(self, track_id):
        state = self._state[track_id]
        return state["blinks"], state["blinks"] >= self.min_blinks

    def passed(self, track_id):
        return self._state[track_id]["blinks"] >= self.min_blinks

    def blink_count(self, track_id):
        return self._state[track_id]["blinks"]

    def active(self, track_id):
        return track_id in self._state

    def reset(self, track_id):
        self._state.pop(track_id, None)

    def clear(self):
        self._state.clear()
