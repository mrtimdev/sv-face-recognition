"""Face templates built from the angle images stored in SV HRM: what the Live Monitor matches against.

HRM holds the images. This keeps one SFace template per angle in a private file next to
employees.json, remembers each image's checksum so only changed images are downloaded again, and
leaves the file untouched when nothing changed, so a sync doesn't reload the Live Monitor.
"""
import base64
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import hrm_sync
from .template_store import TEMPLATE_MODEL

STORE_NAME = "hrm_angle_templates.json"
STORE_VERSION = 1


@dataclass(frozen=True)
class SyncResult:
    employees: int
    changed: bool
    problems: tuple = ()


def store_path(settings):
    return Path(settings.employees_path).with_name(STORE_NAME)


def _vector_text(vector):
    return base64.b64encode(np.asarray(vector, dtype="<f8").tobytes()).decode("ascii")


def vector_of(entry):
    """The template in a store entry, or None for an angle that couldn't be used."""
    text = entry.get("vector") if isinstance(entry, dict) else None
    if not text:
        return None
    try:
        vector = np.frombuffer(base64.b64decode(text), dtype="<f8")
    except (TypeError, ValueError):
        return None
    return vector if vector.shape == (128,) and np.isfinite(vector).all() else None


def load(path):
    """{employee code: {angle: entry}}; empty for a missing, damaged or other-model file, which a sync rebuilds."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logging.warning("Ignoring the unreadable face angle templates: %s", exc)
        return {}
    if (not isinstance(data, dict) or data.get("version") != STORE_VERSION
            or data.get("model") != TEMPLATE_MODEL or not isinstance(data.get("employees"), dict)):
        return {}
    return {code: angles for code, angles in data["employees"].items() if isinstance(angles, dict)}


def _save(path, employees):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        # mkstemp creates the file for this user only; face templates are biometric data.
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"version": STORE_VERSION, "model": TEMPLATE_MODEL, "employees": employees}, stream,
                      ensure_ascii=False)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def counts(store):
    """{casefolded employee code: usable angles}, for showing who the Live Monitor can recognize."""
    result = {}
    for code, angles in store.items():
        usable = sum(1 for entry in angles.values() if vector_of(entry) is not None)
        if usable:
            result[str(code).strip().casefold()] = usable
    return result


def problems_for(store, employee_code):
    """{angle: why it can't be matched} for one employee."""
    wanted = str(employee_code or "").strip().casefold()
    for code, angles in store.items():
        if str(code).strip().casefold() == wanted:
            return {angle: entry.get("problem") or "It couldn't be read"
                    for angle, entry in angles.items() if vector_of(entry) is None}
    return {}


def encode_angle(backend, content):
    """(template, "") for an image showing exactly one face, else (None, why not)."""
    from .enrollment import detect_faces, encode_faces    # enrollment imports catalog, which imports this.
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR) if content else None
    if image is None:
        return None, "The image can't be read"
    boxes = detect_faces(backend, image)
    if not boxes:
        return None, "No face found in the image"
    if len(boxes) > 1:
        return None, "More than one face in the image"
    return np.asarray(encode_faces(backend, image, boxes)[0], dtype=np.float64), ""


def sync(config, path, backend=None, opener=None):
    """Bring the store in line with HRM. Raises ``hrm_sync.HRMError`` when the list can't be read.

    An image that fails to download keeps its previous template and is tried again next time; one
    that has no usable face is remembered until HRM holds a different image.
    """
    old = load(path)
    rows = hrm_sync.angle_summary(config, opener=opener)
    new, problems = {}, []
    for row in rows:
        code, angle, sha = (str(row.get(key) or "").strip() for key in ("employeeCode", "angle", "sha256"))
        if not code or not angle:
            continue
        prior = old.get(code, {}).get(angle)
        if isinstance(prior, dict) and sha and prior.get("sha256") == sha:
            new.setdefault(code, {})[angle] = prior
            continue
        label = f"{code} {angle}"
        try:
            content = hrm_sync.angle_image(config, code, angle, opener=opener)
            if sha and hashlib.sha256(content).hexdigest() != sha:
                raise hrm_sync.HRMError("the image doesn't match its checksum")
            if backend is None:
                from .face_backend import OpenCVFaceBackend
                backend = OpenCVFaceBackend()
            vector, problem = encode_angle(backend, content)
        except (hrm_sync.HRMError, OSError, ValueError, cv2.error) as exc:
            problems.append(f"{label}: {exc}")
            if isinstance(prior, dict):
                new.setdefault(code, {})[angle] = prior
            continue
        if problem:
            problems.append(f"{label}: {problem}")
        new.setdefault(code, {})[angle] = {"sha256": sha, "version": row.get("version"), "problem": problem,
                                           "vector": None if vector is None else _vector_text(vector)}
    changed = new != old
    if changed or not Path(path).exists():
        _save(path, new)
    return SyncResult(sum(1 for angles in new.values() if any(vector_of(e) is not None for e in angles.values())),
                      changed, tuple(problems))
