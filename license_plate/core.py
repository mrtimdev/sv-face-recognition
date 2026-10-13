"""FastALPR adapter and conservative capture policy (no GUI dependency)."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import uuid

import cv2


@dataclass(frozen=True)
class Plate:
    text: str
    confidence: float
    box: tuple


def create_recognizer():
    try:
        from fast_alpr import ALPR
    except ImportError as exc:
        raise RuntimeError("Install plate support: python -m pip install -r requirements-plates.txt") from exc
    return ALPR(detector_model="yolo-v9-t-384-license-plate-end2end",
                ocr_model="cct-xs-v2-global-model", ocr_device="cpu",
                detector_providers=["CPUExecutionProvider"])


def read_plates(recognizer, frame, threshold):
    plates = []
    height, width = frame.shape[:2]
    for result in recognizer.predict(frame):
        ocr = result.ocr
        if ocr is None or not ocr.text:
            continue
        values = ocr.confidence
        if isinstance(values, (list, tuple)):
            confidence = sum(values) / len(values) if values else 0.0
        else:
            confidence = float(values or 0)
        box = result.detection.bounding_box
        bounds = (max(0, int(box.x1)), max(0, int(box.y1)),
                  min(width, int(box.x2)), min(height, int(box.y2)))
        text = ocr.text.strip().upper()
        if (text and math.isfinite(confidence) and confidence >= threshold
                and bounds[2] > bounds[0] and bounds[3] > bounds[1]):
            plates.append(Plate(text, confidence, bounds))
    return plates


class CapturePolicy:
    """Require consecutive OCR confirmations, then suppress repeat captures."""
    def __init__(self, confirmations=3, cooldown=30):
        self.confirmations = confirmations
        self.cooldown = cooldown
        self.counts = {}
        self.saved = {}

    def eligible(self, plates, now):
        present = {plate.text for plate in plates}
        self.counts = {text: self.counts.get(text, 0) + 1 for text in present}
        self.saved = {text: stamp for text, stamp in self.saved.items()
                      if now - stamp < self.cooldown}
        return [plate for plate in plates if self.counts[plate.text] >= self.confirmations
                and plate.text not in self.saved]

    def mark_saved(self, plate, now):
        self.saved[plate.text] = now


def save_capture(directory, frame, plate, source, automatic):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc)
    key = stamp.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
    full = directory / (key + ".jpg")
    crop = directory / (key + "-plate.jpg")
    x1, y1, x2, y2 = plate.box
    try:
        if not cv2.imwrite(str(full), frame) or not cv2.imwrite(str(crop), frame[y1:y2, x1:x2]):
            raise OSError("Could not save plate images")
        record = dict(timestamp=stamp.isoformat(), text=plate.text,
                      confidence=plate.confidence, box=plate.box, source=str(source),
                      automatic=automatic, image=full.name, crop=crop.name)
        with (directory / "captures.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        full.unlink(missing_ok=True)
        crop.unlink(missing_ok=True)
        raise
    return record
