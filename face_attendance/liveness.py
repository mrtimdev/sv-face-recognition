"""Multi-layered anti-spoofing: blink detection, texture analysis, color
consistency, focus variation, moiré detection, and micro-motion tracking.

A printed photo or screen replay cannot produce natural blinks, has flat
texture (uniform LBP), screen-like color artifacts, and no natural
micro-motion.  Requiring evidence from multiple independent signals blocks
the most common presentation attacks.
"""
import math
from collections import defaultdict, deque

import cv2
import numpy as np


EAR_BLINK_THRESHOLD = 0.21
EAR_OPEN_THRESHOLD = 0.26
MIN_BLINKS = 1

TEXTURE_SCORE_THRESHOLD = 0.45
COLOR_SCORE_THRESHOLD = 0.35
MIN_TEXTURE_SAMPLES = 3
MIN_COLOR_SAMPLES = 3
MIN_MOTION_SAMPLES = 5
MOTION_VARIANCE_THRESHOLD = 0.5


def _ear(eye_points):
    if len(eye_points) != 6:
        return 1.0
    p1, p2, p3, p4, p5, p6 = eye_points
    vertical_a = math.dist(p2, p6)
    vertical_b = math.dist(p3, p5)
    horizontal = math.dist(p1, p4)
    if horizontal < 1e-6:
        return 1.0
    return (vertical_a + vertical_b) / (2.0 * horizontal)


def texture_score(face_bgr):
    """LBP entropy of the face region.  Real skin has diverse micro-texture
    (high entropy); photos and screens produce more uniform patterns."""
    if face_bgr is None or face_bgr.size < 100:
        return 0.0
    gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
    padded = cv2.copyMakeBorder(gray, 1, 1, 1, 1, cv2.BORDER_REFLECT)
    center = padded[1:-1, 1:-1].astype(np.int16)
    lbp = np.zeros_like(center, dtype=np.uint8)
    for bit, (dy, dx) in enumerate([(-1, -1), (-1, 0), (-1, 1), (0, 1),
                                     (1, 1), (1, 0), (1, -1), (0, -1)]):
        ny, nx = 1 + dy, 1 + dx
        neighbor = padded[ny:ny + 64, nx:nx + 64].astype(np.int16)
        lbp |= ((neighbor >= center).astype(np.uint8) << bit)
    hist = np.bincount(lbp.ravel(), minlength=256).astype(np.float32)
    total = hist.sum()
    if total < 1:
        return 0.0
    hist /= total
    nonzero = hist[hist > 0]
    entropy = float(-np.sum(nonzero * np.log2(nonzero)))
    return min(1.0, entropy / 6.5)


def color_score(face_bgr):
    """Skin-colour naturalness in YCrCb.  Real skin sits in a specific
    chrominance range with natural variance; screens and prints often fall
    outside or show unnaturally uniform distributions."""
    if face_bgr is None or face_bgr.size < 100:
        return 0.0
    ycrcb = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2YCrCb)
    cr = ycrcb[:, :, 1].astype(np.float64)
    cb = ycrcb[:, :, 2].astype(np.float64)
    skin_mask = (cr >= 133) & (cr <= 173) & (cb >= 77) & (cb <= 127)
    skin_ratio = float(np.count_nonzero(skin_mask)) / max(1, skin_mask.size)
    coverage = min(1.0, skin_ratio / 0.3)
    if np.any(skin_mask):
        cr_std = float(np.std(cr[skin_mask]))
        cb_std = float(np.std(cb[skin_mask]))
    else:
        cr_std = cb_std = 0.0
    variance = min(1.0, (cr_std + cb_std) / 30.0)
    return 0.6 * coverage + 0.4 * variance


def focus_variance(face_bgr):
    """Laplacian variance across face sub-regions.  Real faces show depth-
    dependent focus variation; flat photos are uniformly sharp or blurry."""
    if face_bgr is None or face_bgr.size < 100:
        return 0.0
    gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
    h = gray.shape[0] // 3
    variances = [float(cv2.Laplacian(gray[i * h:(i + 1) * h, :], cv2.CV_64F).var())
                 for i in range(3)]
    spread = max(variances) - min(variances) if len(variances) >= 2 else 0.0
    avg = sum(variances) / max(1, len(variances))
    if avg < 15.0:
        return 0.3
    return min(1.0, 0.5 + spread / max(1.0, avg))


def moire_score(face_bgr):
    """Detect moiré patterns from screen pixel grids via FFT peak analysis."""
    if face_bgr is None or face_bgr.size < 100:
        return 1.0
    gray = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA)
    f = np.fft.fft2(gray.astype(np.float32))
    fshift = np.fft.fftshift(f)
    magnitude = np.log1p(np.abs(fshift))
    center = magnitude.shape[0] // 2
    mask = np.ones_like(magnitude, dtype=bool)
    mask[center - 3:center + 4, center - 3:center + 4] = False
    high_freq = magnitude[mask]
    mean_val = float(np.mean(high_freq))
    if mean_val < 1e-6:
        return 1.0
    peak_ratio = float(np.max(high_freq)) / mean_val
    if peak_ratio > 8.0:
        return 0.2
    if peak_ratio > 5.0:
        return 0.5
    return 1.0


class LivenessChecker:
    """Multi-signal anti-spoofing combining blink detection, texture analysis,
    colour consistency, focus variation, moiré detection, and micro-motion."""

    def __init__(self, blink_threshold=EAR_BLINK_THRESHOLD,
                 open_threshold=EAR_OPEN_THRESHOLD,
                 min_blinks=MIN_BLINKS):
        self.blink_threshold = blink_threshold
        self.open_threshold = open_threshold
        self.min_blinks = min_blinks
        self._state = defaultdict(lambda: {
            "blinks": 0, "closed": False,
            "ear_history": deque(maxlen=30),
            "texture_scores": deque(maxlen=10),
            "color_scores": deque(maxlen=10),
            "focus_scores": deque(maxlen=10),
            "moire_scores": deque(maxlen=5),
            "motion_variances": deque(maxlen=15),
            "passed": False,
        })

    def update(self, track_id, landmarks):
        left_eye = landmarks.get("left_eye", [])
        right_eye = landmarks.get("right_eye", [])
        if not left_eye or not right_eye:
            return self._blink_result(track_id)
        ear = (_ear(left_eye) + _ear(right_eye)) / 2.0
        state = self._state[track_id]
        state["ear_history"].append(ear)
        if ear < self.blink_threshold:
            state["closed"] = True
        elif ear > self.open_threshold and state["closed"]:
            state["closed"] = False
            state["blinks"] += 1
        return self._blink_result(track_id)

    def update_texture(self, track_id, face_roi):
        """Feed a BGR face ROI for texture, colour, focus and moiré analysis."""
        if face_roi is None or face_roi.size < 100:
            return
        state = self._state[track_id]
        if state["passed"]:
            return
        state["texture_scores"].append(texture_score(face_roi))
        state["color_scores"].append(color_score(face_roi))
        state["focus_scores"].append(focus_variance(face_roi))
        state["moire_scores"].append(moire_score(face_roi))

    def update_motion(self, track_id, flow_variance):
        """Feed optical-flow variance within the face bounding box."""
        state = self._state[track_id]
        if not state["passed"]:
            state["motion_variances"].append(flow_variance)

    def _blink_result(self, track_id):
        state = self._state[track_id]
        return state["blinks"], self.is_live(track_id)

    def is_live(self, track_id):
        """Combined liveness decision from all accumulated signals."""
        state = self._state[track_id]
        if state["passed"]:
            return True

        blink_ok = state["blinks"] >= self.min_blinks
        has_landmarks = len(state["ear_history"]) > 0

        ear_history = state["ear_history"]
        ear_var_ok = True
        if len(ear_history) >= 10:
            ear_var_ok = float(np.std(list(ear_history))) > 0.008

        tex = state["texture_scores"]
        texture_ok = (len(tex) >= MIN_TEXTURE_SAMPLES
                      and float(np.median(list(tex))) >= TEXTURE_SCORE_THRESHOLD)

        col = state["color_scores"]
        color_ok = (len(col) >= MIN_COLOR_SAMPLES
                    and float(np.median(list(col))) >= COLOR_SCORE_THRESHOLD)

        foc = state["focus_scores"]
        focus_ok = len(foc) < 3 or float(np.median(list(foc))) >= 0.4

        moire = state["moire_scores"]
        moire_ok = len(moire) < 2 or float(np.median(list(moire))) >= 0.4

        motion = state["motion_variances"]
        motion_ok = (len(motion) < MIN_MOTION_SAMPLES
                     or float(np.median(list(motion))) >= MOTION_VARIANCE_THRESHOLD)

        if has_landmarks:
            hard_pass = blink_ok and texture_ok and color_ok
            soft_score = sum([ear_var_ok, focus_ok, moire_ok, motion_ok])
        else:
            hard_pass = texture_ok and color_ok
            soft_score = sum([focus_ok, moire_ok, motion_ok])
        if hard_pass and soft_score >= 2:
            state["passed"] = True
            return True
        return False

    def passed(self, track_id):
        return self._state.get(track_id, {}).get("passed", False)

    def blink_count(self, track_id):
        return self._state.get(track_id, {}).get("blinks", 0)

    def active(self, track_id):
        return track_id in self._state

    def reset(self, track_id):
        self._state.pop(track_id, None)

    def clear(self):
        self._state.clear()
