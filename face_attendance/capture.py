"""Per-person display crops made only after identity and liveness authorize capture."""
import math

import numpy as np


def face_capture(frame, box, neighbors=(), padding=.20):
    """Return owned pixels and their source rectangle (top, right, bottom, left).

    Use the box from the analyzed frame. Padding is limited at nearby faces;
    overlapping face boxes are deferred rather than saving mixed evidence.
    This crop is never used as input to recognition or anti-spoofing.
    """
    if (not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] != 3
            or frame.dtype != np.uint8 or frame.size == 0):
        raise ValueError("Invalid capture frame")
    height, width = frame.shape[:2]

    def valid_box(value, clip=False):
        if value is None or len(value) != 4:
            raise ValueError("Missing face coordinates")
        t, r, b, l = map(float, value)
        if not all(math.isfinite(v) for v in (t, r, b, l)) or t >= b or l >= r:
            raise ValueError("Invalid face coordinates")
        if clip:
            return max(0, t), min(width, r), min(height, b), max(0, l)
        if not (0 <= t < b <= height and 0 <= l < r <= width):
            raise ValueError("Invalid face coordinates")
        return t, r, b, l

    top, right, bottom, left = valid_box(box)
    dx, dy = (right - left) * padding, (bottom - top) * padding
    x1, y1 = max(0, left - dx), max(0, top - dy)
    x2, y2 = min(width, right + dx), min(height, bottom + dy)
    for other in neighbors:
        ot, oright, ob, ol = valid_box(other, clip=True)
        if ot >= ob or ol >= oright:
            continue
        if min(bottom, ob) > max(top, ot) and min(right, oright) > max(left, ol):
            raise ValueError("Separate overlapping faces before capture")
        if min(y2, ob) <= max(y1, ot) or min(x2, oright) <= max(x1, ol):
            continue
        if ol >= right:
            x2 = min(x2, math.floor((right + ol) / 2))
        if oright <= left:
            x1 = max(x1, math.ceil((left + oright) / 2))
        if ot >= bottom:
            y2 = min(y2, math.floor((bottom + ot) / 2))
        if ob <= top:
            y1 = max(y1, math.ceil((top + ob) / 2))
    x1, y1, x2, y2 = math.floor(x1), math.floor(y1), math.ceil(x2), math.ceil(y2)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        raise ValueError("Empty face capture")
    return crop.copy(), (y1, x2, y2, x1)
