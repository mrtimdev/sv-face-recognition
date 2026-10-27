"""Separate unverified face observations; never attendance or enrollment records."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2


def save_unknown_capture(directory, crop, box, track_id, age, wall_time,
                         category="unknown_not_enrolled", employee_id="", employee_name="",
                         liveness_verified=False, presentation_verified=False):
    if category not in ("unknown_not_enrolled", "inactive_employee"):
        raise ValueError("Invalid observation category")
    stamp = datetime.fromtimestamp(wall_time, timezone.utc).isoformat()
    event_id = uuid4().hex
    root = Path(directory) / ("inactive" if category == "inactive_employee" else "unknown")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = root / (".pending-" + event_id)
    target = root / event_id
    temporary.mkdir(mode=0o700)
    info = {"event_id": event_id, "category": category,
            "employee_id": employee_id, "employee_name": employee_name,
            "attendance_blocked": category == "inactive_employee",
            "track_id": track_id, "age": round(age, 1), "captured_at": stamp,
            "identity_verified": category == "inactive_employee",
            "liveness_verified": bool(liveness_verified),
            "presentation_verified": bool(presentation_verified),
            "face_box": list(box), "snapshot": str(target / "face.jpg")}
    try:
        if not cv2.imwrite(str(temporary / "face.jpg"), crop):
            raise OSError("Could not encode unknown face photo")
        (temporary / "record.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        temporary.rename(target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return info
