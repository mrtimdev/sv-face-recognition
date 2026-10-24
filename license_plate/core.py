"""FastALPR adapter and conservative capture policy (no GUI dependency)."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import uuid

import cv2

from face_attendance.settings import describe_source
from face_attendance.storage import MIN_DISK_MB, write_jpeg


@dataclass(frozen=True)
class Plate:
    text: str
    confidence: float
    box: tuple
    track_id: int = 0


def create_recognizer():
    isolated = os.environ.get('SV_PLATE_PYTHON')
    if not isolated:
        root = Path(__file__).resolve().parents[1]
        candidate = root / '.venv-plates' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
        if candidate.is_file():
            isolated = str(candidate)
    if isolated:
        from .process_backend import ProcessRecognizer
        return ProcessRecognizer(isolated)
    try:
        from fast_alpr import ALPR
    except ImportError as exc:
        raise RuntimeError("Install plate support in a separate .venv-plates environment; see OPERATIONS.md") from exc
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
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if shutil.disk_usage(directory).free < MIN_DISK_MB * 1024 * 1024 + frame.nbytes * 2:
        raise OSError("Disk space low; plate capture was not saved")
    stamp = datetime.now(timezone.utc)
    key = stamp.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
    full = directory / (key + ".jpg")
    crop = directory / (key + "-plate.jpg")
    x1, y1, x2, y2 = plate.box
    try:
        write_jpeg(full, frame)
        write_jpeg(crop, frame[y1:y2, x1:x2])
        record = dict(timestamp=stamp.isoformat(), text=plate.text,
                      confidence=plate.confidence, box=plate.box, source=describe_source(source),
                      automatic=automatic, image=full.name, crop=crop.name,
                      track_id=plate.track_id)
        descriptor = os.open(directory / "captures.jsonl", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            if hasattr(os, 'fchmod'):
                os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        full.unlink(missing_ok=True)
        crop.unlink(missing_ok=True)
        raise
    return record


class PlateTracker:
    """Session IDs by exact normalized OCR; expires after absence (not vehicle tracking)."""
    def __init__(self, timeout=10):
        self.timeout = timeout
        self.active = {}
        self.next_id = 1

    def update(self, plates, now):
        from dataclasses import replace
        from .maintenance import plate_key
        self.active = {key: value for key, value in self.active.items()
                       if now - value[1] < self.timeout}
        result = []
        for plate in plates:
            key = plate_key(plate.text)
            if key not in self.active:
                self.active[key] = (self.next_id, now)
                self.next_id += 1
            track_id, _ = self.active[key]
            self.active[key] = (track_id, now)
            result.append(replace(plate, track_id=track_id))
        return result


def load_captures(directory, limit=200):
    from collections import deque
    path = Path(directory) / "captures.jsonl"
    records = deque(maxlen=limit)
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if all(key in record for key in ("text", "timestamp", "confidence", "image", "crop")):
                        datetime.fromisoformat(record["timestamp"])
                        float(record["confidence"])
                        records.append(record)
                except (ValueError, TypeError):
                    continue
    return list(records)
