"""Local MiniFASNet presentation-attack detection, independent of expressions.

The two Apache-2.0 models are bundled with the app; no camera data is uploaded.
This is probabilistic RGB PAD, not proof of physical presence or certification.
See assets/anti_spoof/NOTICE.md for provenance, hashes and limitations.
"""
import hashlib
import math
from pathlib import Path

import cv2
import numpy as np


MODEL_DIR = Path(__file__).resolve().parent / "assets" / "anti_spoof"
MODELS = (
    ("MiniFASNetV2.onnx", 2.7, "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907"),
    ("MiniFASNetV1SE.onnx", 4.0, "ebab7f90c7833fbccd46d3a555410e78d969db5438e169b6524be444862b3676"),
)
LIVE_THRESHOLD = 0.80  # Each model must meet the threshold; never average away a rejection.
SAMPLE_MAX_AGE = 0.75
MIN_SAMPLES = 3
MIN_SAMPLE_SPAN = 0.35
MIN_FACE_PIXELS = 80


def face_patch(frame, box, scale):
    """Expanded BGR crop with edge shifting, following upstream crop geometry."""
    height, width = frame.shape[:2]
    top, right, bottom, left = map(float, box)
    if not all(math.isfinite(v) for v in (top, right, bottom, left)):
        raise ValueError("invalid face coordinates")
    face_w, face_h = right - left, bottom - top
    if (min(face_w, face_h) < MIN_FACE_PIXELS or top < 0 or left < 0
            or bottom > height or right > width):
        raise ValueError("Move closer and keep your whole face visible")
    factor = min(scale, (width - 1) / face_w, (height - 1) / face_h)
    patch_w, patch_h = face_w * factor, face_h * factor
    x = min(max(0., (left + right - patch_w) / 2), width - 1 - patch_w)
    y = min(max(0., (top + bottom - patch_h) / 2), height - 1 - patch_h)
    crop = frame[int(y):int(y + patch_h) + 1, int(x):int(x + patch_w) + 1]
    if crop.size == 0:
        raise ValueError("empty face crop")
    # Upstream models expect raw 0..255 BGR, not RGB or normalized pixels.
    return cv2.resize(crop, (80, 80), interpolation=cv2.INTER_LINEAR)


class AntiSpoofService:
    """Owned by the recognition worker; OpenCV DNN nets must not be shared."""

    def __init__(self, model_dir=MODEL_DIR):
        self.nets = []
        self.error = ""
        try:
            for filename, scale, digest in MODELS:
                path = Path(model_dir) / filename
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise ValueError("model checksum mismatch")
                net = cv2.dnn.readNetFromONNX(str(path))
                net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
                self.nets.append((net, scale))
        except (OSError, ValueError, cv2.error):
            self.nets = []
            self.error = "Anti-spoof models unavailable - reinstall application"

    def evaluate(self, frame, box):
        """Return conservative live confidence, or None on any failure."""
        if self.error:
            return None, self.error
        try:
            scores = []
            for net, scale in self.nets:
                patch = face_patch(frame, box, scale)
                blob = cv2.dnn.blobFromImage(patch, scalefactor=1.0, swapRB=False, crop=False)
                net.setInput(blob)
                logits = np.asarray(net.forward(), dtype=float).reshape(-1)
                if logits.shape != (3,) or not np.all(np.isfinite(logits)):
                    raise ValueError("invalid anti-spoof output")
                probability = np.exp(logits - np.max(logits))
                probability /= probability.sum()
                scores.append(float(probability[1]))  # Upstream class 1 = live.
            if len(scores) != len(MODELS):
                raise ValueError("incomplete anti-spoof ensemble")
            return min(scores), ""
        except ValueError as exc:
            return None, str(exc)
        except cv2.error:
            return None, "Anti-spoof check failed - attendance blocked"


class PresentationGuard:
    """Require sustained, current model agreement; reject missing evidence."""

    def __init__(self):
        self._state = {}

    def update(self, track_id, score, captured_at):
        state = self._state.get(track_id)
        if state and captured_at <= state["last"]:
            return False
        if score is None or not math.isfinite(score) or not LIVE_THRESHOLD <= score <= 1:
            self.reset(track_id)
            return False
        if state is None or captured_at - state["last"] > SAMPLE_MAX_AGE:
            state = self._state[track_id] = {"first": captured_at, "last": captured_at, "count": 0}
        state["last"] = captured_at
        state["count"] += 1
        return self.passed(track_id, captured_at)

    def passed(self, track_id, now):
        state = self._state.get(track_id)
        if state is None:
            return False
        if not 0 <= now - state["last"] <= SAMPLE_MAX_AGE:
            self.reset(track_id)
            return False
        return state["count"] >= MIN_SAMPLES and state["last"] - state["first"] >= MIN_SAMPLE_SPAN

    def reset(self, track_id):
        self._state.pop(track_id, None)

    def clear(self):
        self._state.clear()
