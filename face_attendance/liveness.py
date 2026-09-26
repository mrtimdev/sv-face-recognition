"""Configurable adaptive expression verification for an ordinary RGB camera.

A brief neutral baseline distinguishes expression changes from a static photo.
This convenience check is not validated PAD: recorded blinks or smiles can
satisfy it, so it must not be advertised as video-replay protection.
"""
import math
import time
from collections import deque

import cv2
import numpy as np

from .config import ATTENDANCE_MODES


MAX_SAMPLE_GAP = 0.75
CHALLENGE_TIMEOUT = 25.0
PASS_VALID_SEC = 10.0
MIN_EYE_WIDTH = 12.0
CALIBRATION_SEC = 0.35
MIN_BLINK_SEC = 0.04
MAX_BLINK_SEC = 0.80
SMILE_HOLD_SEC = 0.25

TEXTURE_SCORE_THRESHOLD = 0.45
MIN_TEXTURE_SAMPLES = 3


def _ear(eye_points):
    if len(eye_points) != 6:
        return None
    p1, p2, p3, p4, p5, p6 = eye_points
    vertical_a = math.dist(p2, p6)
    vertical_b = math.dist(p3, p5)
    horizontal = math.dist(p1, p4)
    if horizontal < MIN_EYE_WIDTH:
        return None
    return (vertical_a + vertical_b) / (2.0 * horizontal)


def texture_score(face_bgr):
    """LBP entropy heuristic. Detailed photos can also score highly."""
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
    """Legacy YCrCb colour diagnostic, never used to authorize liveness."""
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
    """Regional sharpness diagnostic; this is not a depth measurement."""
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


def _measure(landmarks):
    """Eye ratios plus optional scale/roll-invariant smile measurements."""
    try:
        left, right = landmarks["left_eye"], landmarks["right_eye"]
        eyes = (_ear(left), _ear(right))
        if any(ear is None or not math.isfinite(ear) or not 0 <= ear <= .8 for ear in eyes):
            return None
        left = np.mean(np.asarray(left, dtype=float), axis=0)
        right = np.mean(np.asarray(right, dtype=float), axis=0)
        if left[0] > right[0]:
            left, right = right, left
        axis = right - left
        span = float(np.linalg.norm(axis))
        if span < 25:
            return None
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    smile = None
    try:
        nose = np.asarray(landmarks["nose_bridge"][-1], dtype=float)
        top, bottom = landmarks["top_lip"], landmarks["bottom_lip"]
        corners = np.asarray([top[0], top[6]], dtype=float)
        center = (np.asarray(top[9], dtype=float) + np.asarray(bottom[9], dtype=float)) / 2
        down = np.array([-axis[1], axis[0]]) / span
        yaw = float(np.dot(nose - (left + right) / 2, axis)) / (span * span)
        width = float(np.linalg.norm(corners[1] - corners[0])) / span
        lift = float(np.dot(center - np.mean(corners, axis=0), down)) / span
        opening = math.dist(top[9], bottom[9]) / span
        if all(math.isfinite(v) for v in (yaw, width, lift, opening)) and .15 < width < 1.5:
            smile = (yaw, width, lift, opening)
    except (KeyError, IndexError, TypeError, ValueError):
        pass  # Lips are optional for the blink route.
    return eyes, smile


class LivenessChecker:
    """Fresh face evidence with the configured expression requirement."""

    def __init__(self, mode="blink_or_smile"):
        # Keep the legacy either-expression API for standalone callers.
        if mode not in (*ATTENDANCE_MODES, "blink_or_smile"):
            raise ValueError("Invalid attendance requirement")
        self.mode = mode
        self._state = {}

    @staticmethod
    def _new(now):
        return {
            "phase": "calibrate", "started": now,
            "last_sample": None, "last_input": None, "valid": False,
            "calibration": [], "baseline_eyes": None, "baseline_smile": None,
            "open_since": None, "closed_since": None,
            "smile_since": None, "smile_samples": 0,
            "passed_at": None, "method": None,
            "blink_done": False, "smile_done": False,
            "texture_scores": deque(maxlen=5), "moire_scores": deque(maxlen=5),
            "image_valid": False, "last_texture": None,
        }

    @staticmethod
    def _break_hold(state):
        state["open_since"] = state["closed_since"] = None
        state["smile_since"] = None
        state["smile_samples"] = 0
        state["calibration"].clear()

    def update(self, track_id, landmarks, now=None):
        now = time.monotonic() if now is None else now
        state = self._state.get(track_id)
        if state is not None:
            if state["last_input"] is not None and now <= state["last_input"]:
                return int(state["phase"] == "complete"), False
            last = state["last_sample"]
            if ((last is not None and now - last > MAX_SAMPLE_GAP)
                    or (state["passed_at"] is None and now - state["started"] > CHALLENGE_TIMEOUT)
                    or (state["passed_at"] is not None and now - state["passed_at"] > PASS_VALID_SEC)):
                self.reset(track_id)
                state = None
        if state is None:
            state = self._state[track_id] = self._new(now)
        state["last_input"] = now
        if self.mode == "face":
            # Current face image + identity + presentation checks are still
            # required by the tracker and attendance service; no expression.
            state["valid"] = True
            state["last_sample"] = now
            state["phase"], state["method"] = "complete", "face"
            return 1, self.is_live(track_id, now)
        measured = _measure(landmarks or {})
        if measured is None:
            if state["phase"] == "complete":
                self.reset(track_id)
                return 0, False
            state["valid"] = False
            self._break_hold(state)
            return 0, False
        state["valid"] = True
        state["last_sample"] = now
        eyes, smile = measured
        if smile is None and self.mode in ("smile", "blink_and_smile"):
            if state["phase"] == "complete":
                self.reset(track_id)
                return 0, False
            state["valid"] = False
            self._break_hold(state)
            return 0, False
        if state["phase"] == "calibrate":
            # No universal 0.26 open-eye threshold: learn each eye separately.
            if min(eyes) < .10:
                self._break_hold(state)
                return 0, False
            state["calibration"].append((now, eyes, smile))
            samples = state["calibration"]
            if len(samples) >= 3 and now - samples[0][0] >= CALIBRATION_SEC:
                # A single enlarged eyelid estimate must not make normal open
                # eyes look closed for the rest of the challenge.
                state["baseline_eyes"] = tuple(np.median([s[1] for s in samples], axis=0))
                mouths = [s[2] for s in samples if s[2] is not None]
                state["baseline_smile"] = tuple(np.median(mouths, axis=0)) if len(mouths) >= 3 else None
                state["phase"] = "ready"
                self._break_hold(state)
                state["open_since"] = now - CALIBRATION_SEC
        elif state["phase"] == "ready":
            baseline = state["baseline_eyes"]
            closed = all(ear <= base * .65 and base - ear >= .035 for ear, base in zip(eyes, baseline))
            opened = all(ear >= base * .85 for ear, base in zip(eyes, baseline))
            if closed:
                if (state["closed_since"] is None and state["open_since"] is not None
                        and now - state["open_since"] >= .08):
                    state["closed_since"] = now
                state["open_since"] = None
            elif opened:
                if state["closed_since"] is not None:
                    duration = now - state["closed_since"]
                    if MIN_BLINK_SEC <= duration <= MAX_BLINK_SEC:
                        state["blink_done"] = True
                    state["closed_since"] = None
                if state["open_since"] is None:
                    state["open_since"] = now
                # Keep the calibrated baseline: a single landmark outlier
                # must not permanently raise the open-eye threshold.
            elif any(ear >= base * .85 for ear, base in zip(eyes, baseline)):
                # A wink or tracking mismatch is not a bilateral blink.
                state["open_since"] = state["closed_since"] = None
            base = state["baseline_smile"]
            smiling = (smile is not None and base is not None
                       and abs(smile[0] - base[0]) <= .10
                       and smile[1] - base[1] >= max(.06, base[1] * .10)
                       and smile[2] - base[2] >= .025
                       # Mouth opening alone must not masquerade as a smile.
                       and smile[3] - base[3] <= .12)
            if smiling:
                if state["smile_since"] is None:
                    state["smile_since"] = now
                state["smile_samples"] += 1
                if state["smile_samples"] >= 3 and now - state["smile_since"] >= SMILE_HOLD_SEC:
                    state["smile_done"] = True
            else:
                state["smile_since"] = None
                state["smile_samples"] = 0
            blink, smiled = state["blink_done"], state["smile_done"]
            complete = {"blink": blink, "smile": smiled,
                        "blink_and_smile": blink and smiled,
                        "blink_or_smile": blink or smiled}[self.mode]
            if complete:
                state["phase"] = "complete"
                state["method"] = ("blink_and_smile" if self.mode == "blink_and_smile"
                                   else "smile" if self.mode == "smile" or not blink else "blink")
        return int(state["phase"] == "complete"), self.is_live(track_id, now)

    def update_texture(self, track_id, face_roi, now=None):
        state = self._state.get(track_id)
        if state is None:
            return
        if face_roi is None or face_roi.size < 100:
            state["image_valid"] = False
            if state["phase"] == "complete":
                self.reset(track_id)
            return
        now = time.monotonic() if now is None else now
        state["image_valid"] = True
        if self.mode == "face":
            return
        # Avoid unused colour/focus/optical-flow diagnostics on every frame.
        if state["last_texture"] is not None and now - state["last_texture"] < .25:
            return
        state["last_texture"] = now
        state["texture_scores"].append(texture_score(face_roi))
        state["moire_scores"].append(moire_score(face_roi))

    def is_live(self, track_id, now=None):
        now = time.monotonic() if now is None else now
        state = self._state.get(track_id)
        if state is None or state["last_sample"] is None:
            return False
        if now - state["last_sample"] > MAX_SAMPLE_GAP:
            self.reset(track_id)
            return False
        if state["passed_at"] is not None and now - state["passed_at"] > PASS_VALID_SEC:
            self.reset(track_id)
            return False
        if (now < state["last_sample"] or not state["valid"] or not state["image_valid"]
                or state["phase"] != "complete"):
            return False
        tex, moire = state["texture_scores"], state["moire_scores"]
        if self.mode != "face" and not (
                len(tex) >= MIN_TEXTURE_SAMPLES and np.median(tex) >= TEXTURE_SCORE_THRESHOLD
                and len(moire) >= MIN_TEXTURE_SAMPLES and np.median(moire) >= .4):
            return False
        if state["passed_at"] is None:
            state["passed_at"] = now
        return True

    def passed(self, track_id, now=None):
        return self.is_live(track_id, now)

    def progress(self, track_id):
        state = self._state.get(track_id)
        if not state:
            return 0.0
        if state["phase"] == "complete":
            return 1.0
        if self.mode == "blink_and_smile" and (state["blink_done"] or state["smile_done"]):
            return .65
        return .25 if state["phase"] == "ready" else 0.0

    def prompt(self, track_id):
        state = self._state.get(track_id)
        if not state or not state["valid"]:
            return "Look at the camera - keep eyes visible"
        if state["phase"] == "calibrate":
            return "Look at the camera - relax your face"
        if state["phase"] == "complete":
            return "Hold still - checking"
        if self.mode == "blink_and_smile":
            if state["blink_done"]:
                return "Blink done - now smile"
            if state["smile_done"]:
                return "Smile done - now blink once"
            return "Blink once and smile (either order)"
        return {"blink": "Blink once", "smile": "Smile and hold briefly",
                "face": "Hold still - checking"}.get(self.mode, "Blink once or smile")

    def pause(self, track_id):
        """A missing detection blocks capture; only unfinished verification may resume."""
        state = self._state.get(track_id)
        if state is None:
            return
        if state["phase"] == "complete":
            self.reset(track_id)
        else:
            state["valid"] = False
            self._break_hold(state)

    def active(self, track_id):
        return track_id in self._state

    def reset(self, track_id):
        self._state.pop(track_id, None)

    def clear(self):
        self._state.clear()
