"""Conservative, per-frame image suitability checks; never a liveness score."""
from dataclasses import dataclass
import math

import cv2
import numpy as np


@dataclass(frozen=True)
class QualityResult:
    ok: bool
    prompt: str = ""
    metrics: tuple = ()


class FaceQuality:
    def __init__(self, config):
        self.config = config

    def inspect(self, frame, box):
        """Measure original pixels, before identity encoding or PAD inference."""
        try:
            h, w = frame.shape[:2]
            t, r, b, l = map(float, box)
            if not all(math.isfinite(v) for v in (t, r, b, l)):
                raise ValueError()
            if not (0 <= t < b <= h and 0 <= l < r <= w):
                return QualityResult(False, "Keep your whole face visible")
            if min(b - t, r - l) < self.config.quality_min_face_px:
                return QualityResult(False, "Move closer to the camera")
            roi = frame[math.floor(t):math.ceil(b), math.floor(l):math.ceil(r)]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            # Fixed analysis size makes the threshold less dependent on distance.
            gray = cv2.resize(gray, (96, 96), interpolation=cv2.INTER_AREA)
            brightness = float(np.mean(gray))
            clipped = float(np.mean((gray <= 8) | (gray >= 247)))
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            metrics = (("brightness", brightness), ("clipped_fraction", clipped),
                       ("sharpness", sharpness))
            if brightness < 35 or brightness > 220 or clipped > .55:
                return QualityResult(False, "Improve lighting on your face", metrics)
            if sharpness < self.config.quality_min_sharpness:
                return QualityResult(False, "Hold still - image is blurry", metrics)
            return QualityResult(True, metrics=metrics)
        except (AttributeError, TypeError, ValueError, cv2.error):
            return QualityResult(False, "Face image unavailable")

    def landmarks(self, result, landmarks, box):
        """Frontal-pose guidance from 2D geometry, not calibrated pose angles.

        Mesh confidence is checked by the backend. Plausible landmarks cannot
        prove that eyes/mouth are unoccluded; PAD and expressions stay required.
        """
        if not result.ok:
            return result
        try:
            left = np.mean(np.asarray(landmarks["left_eye"], dtype=float), axis=0)
            right = np.mean(np.asarray(landmarks["right_eye"], dtype=float), axis=0)
            nose = np.asarray(landmarks["nose_bridge"][-1], dtype=float)
            lip = landmarks["top_lip"]
            mouth = (np.asarray(lip[0], dtype=float) + np.asarray(lip[6], dtype=float)) / 2
            pts = np.array([left, right, nose, mouth])
            if pts.shape != (4, 2) or not np.isfinite(pts).all():
                raise ValueError()
            t, r, b, l = box
            if np.any(pts[:, 0] < l) or np.any(pts[:, 0] > r) or np.any(pts[:, 1] < t) or np.any(pts[:, 1] > b):
                raise ValueError()
            if left[0] > right[0]:
                left, right = right, left
            axis = right - left
            span = float(np.linalg.norm(axis))
            if span < 25:
                return QualityResult(False, "Move closer - keep eyes visible", result.metrics)
            axis /= span
            down = np.array([-axis[1], axis[0]])
            center = (left + right) / 2
            mouth_depth = float(np.dot(mouth - center, down))
            if mouth_depth < span * .25:
                raise ValueError()
            yaw = float(np.dot(nose - center, axis)) / span
            pitch = float(np.dot(nose - center, down)) / mouth_depth
            roll = math.degrees(math.atan2(axis[1], axis[0]))
            metrics = result.metrics + (("yaw_ratio", yaw), ("pitch_ratio", pitch), ("roll_deg", roll))
            if abs(yaw) > .32 or not .15 <= pitch <= .85 or abs(roll) > 30:
                return QualityResult(False, "Face the camera - keep your head upright", metrics)
            return QualityResult(True, metrics=metrics)
        except (KeyError, IndexError, TypeError, ValueError):
            return QualityResult(False, "Face the camera - keep eyes and mouth visible", result.metrics)
