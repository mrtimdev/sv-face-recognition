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
LIVE_THRESHOLD = 0.90  # Application policy, not a calibrated probability of liveness.
SAMPLE_MAX_AGE = 0.75
MIN_SAMPLES = 6
MIN_SAMPLE_SPAN = 1.2
MIN_FACE_PIXELS = 80
MIN_CONTEXT_SCALE = 2.0


def live_sample(score):
    return score is not None and math.isfinite(score) and LIVE_THRESHOLD <= score <= 1


def face_patch(frame, box, scale):
    """Preserve context at frame edges using the model author's crop geometry."""
    if (not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3
            or frame.dtype != np.uint8 or frame.size == 0):
        raise ValueError("Anti-spoofing requires an unmodified BGR camera frame")
    height, width = frame.shape[:2]
    top, right, bottom, left = map(float, box)
    if not all(math.isfinite(v) for v in (top, right, bottom, left)):
        raise ValueError("invalid face coordinates")
    face_w, face_h = right - left, bottom - top
    if (min(face_w, face_h) < MIN_FACE_PIXELS or top < 0 or left < 0
            or bottom > height or right > width):
        raise ValueError("Move closer and keep your whole face visible")
    # Convert xyxy to integer xywh before expanding the crop.
    left, top, face_w, face_h = map(int, (left, top, face_w, face_h))
    factor = min(scale, (width - 1) / face_w, (height - 1) / face_h)
    if factor < MIN_CONTEXT_SCALE:
        raise ValueError("Move farther back - show the area around your face")
    patch_w, patch_h = face_w * factor, face_h * factor
    center_x, center_y = left + face_w / 2, top + face_h / 2
    # Shift the expanded rectangle into the image instead of truncating it.
    # Clipping a tall crop at the top/bottom changes both the apparent face
    # scale and its aspect ratio when resized to 80x80. The model author's
    # reference implementation preserves the entire expanded rectangle.
    x1 = max(0., min(center_x - patch_w / 2, width - 1 - patch_w))
    y1 = max(0., min(center_y - patch_h / 2, height - 1 - patch_h))
    x2, y2 = int(x1 + patch_w), int(y1 + patch_h)
    x1, y1 = int(x1), int(y1)
    crop = frame[y1:y2 + 1, x1:x2 + 1]
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
        result = self.inspect(frame, box)
        return result["live_score"], result["error"]

    def inspect(self, frame, box):
        """Include per-model live/attack scores for local failure diagnostics."""
        result = {"live_score": None, "error": "", "models": []}
        if self.error:
            result["error"] = self.error
            return result
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
                result["models"].append({"scale": scale, "live": float(probability[1]),
                                         "attack": float(probability[0] + probability[2]),
                                         "is_real": int(np.argmax(probability)) == 1})
            if len(scores) != len(MODELS):
                raise ValueError("incomplete anti-spoof ensemble")
            result["live_score"] = min(scores)
        except ValueError as exc:
            result["error"] = str(exc)
        except cv2.error:
            result["error"] = "Anti-spoof check failed - attendance blocked"
        return result


class PresentationGuard:
    """Require sustained, current model agreement; reject missing evidence."""

    def __init__(self, min_span=MIN_SAMPLE_SPAN):
        self.min_span = max(MIN_SAMPLE_SPAN, min_span)
        self._state = {}

    def update(self, track_id, score, captured_at):
        state = self._state.get(track_id)
        if state and captured_at <= state["last"]:
            return False
        if not math.isfinite(captured_at) or not live_sample(score):
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
        return state["count"] >= MIN_SAMPLES and state["last"] - state["first"] >= self.min_span

    def progress(self, track_id):
        state = self._state.get(track_id)
        if state is None:
            return 0.0
        return min(1., state["count"] / MIN_SAMPLES, (state["last"] - state["first"]) / self.min_span)

    def reset(self, track_id):
        self._state.pop(track_id, None)

    def clear(self):
        self._state.clear()
