"""Enrollment shared by the command line and the dashboard.

Both entry points reject the same invalid input and write the same on-disk
formats: a versioned SFace sample dictionary and the
optional ``employees.json`` (``{enrollment_name: {employee_id, name}}``).

Nothing here touches the camera, the Qt widgets or the attendance database.
"""
import json
import os
import pickle
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2

from .catalog import legacy_employee_id, load_employee_map
from .template_store import read_templates, template_payload


# Serialize model calls shared by enrollment workers and live recognition.
# OpenCV DNN networks mutate internal input/output buffers during inference.
FACE_BACKEND_LOCK = threading.Lock()


# Samples outside these bounds are refused: a tiny, blurred or badly lit face
# produces an encoding that cannot be matched reliably at the terminal.
MIN_FACE_HEIGHT = 90          # pixels
MIN_BLUR_VARIANCE = 18.0      # variance of the Laplacian over the face region
MIN_BRIGHTNESS = 35.0
MAX_BRIGHTNESS = 225.0


@dataclass(frozen=True)
class EnrollmentOutcome:
    """Result of one enrollment attempt. ``status`` is safe to display."""
    status: str
    message: str = ""
    name: str = ""
    employee_id: str = ""
    employee_name: str = ""
    samples_added: int = 0
    total_samples: int = 0
    total_people: int = 0
    issues: tuple = ()

    @property
    def ok(self):
        return self.status == "enrolled"


def _atomic_write(path, writer, binary=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(mode="w+b" if binary else "w", dir=path.parent,
                                         delete=False, encoding=None if binary else "utf-8")
    temporary = Path(handle.name)
    try:
        writer(handle)
        handle.flush()
        os.fsync(handle.fileno())
    except Exception:
        handle.close()
        temporary.unlink(missing_ok=True)
        raise
    handle.close()
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_encodings(path):
    """Read only explicitly versioned SFace templates, never legacy dlib vectors."""
    return read_templates(path)


def write_encodings(path, data):
    _atomic_write(path, lambda handle: pickle.dump(template_payload(data), handle))


def write_employee(path, name, employee_id):
    """Map an enrollment label to a permanent ID without silently reassigning it."""
    records = load_employee_map(path)
    previous = records.get(name)
    if previous and previous.employee_id != employee_id:
        raise ValueError(f"{name!r} is already mapped to {previous.employee_id}; "
                         "use a deliberate ID migration to change it")
    data = {key: {"employee_id": value.employee_id, "name": value.name}
            for key, value in records.items()}
    canonical = previous.name if previous else next(
        (employee.name for employee in records.values() if employee.employee_id == employee_id), name)
    data[name] = {"employee_id": employee_id, "name": canonical}
    _atomic_write(path, lambda handle: json.dump(data, handle, ensure_ascii=False, indent=2), binary=False)
    return canonical


def detect_faces(backend, image):
    """Face locations for a BGR frame; the backend is injected for testing."""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    with FACE_BACKEND_LOCK:
        return list(backend.face_locations(rgb, model="yunet"))


def encode_faces(backend, image, boxes):
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    with FACE_BACKEND_LOCK:
        return list(backend.face_encodings(rgb, boxes))


def frame_quality(image, box, min_face_height=MIN_FACE_HEIGHT):
    """Human-readable reasons a live-capture sample should not be enrolled."""
    height, width = image.shape[:2]
    top, right, bottom, left = (int(value) for value in box)
    top, bottom = max(0, min(top, height)), max(0, min(bottom, height))
    left, right = max(0, min(left, width)), max(0, min(right, width))
    issues = []
    if bottom - top < min_face_height or right - left < min_face_height // 2:
        issues.append("Move closer to the camera")
    region = image[top:bottom, left:right]
    if region.size == 0:
        issues.append("Face is outside the frame")
        return tuple(issues)
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    brightness = float(gray.mean())
    if brightness < MIN_BRIGHTNESS:
        issues.append("Face is too dark; add more light")
    elif brightness > MAX_BRIGHTNESS:
        issues.append("Face is over-exposed; reduce the light")
    if float(cv2.Laplacian(gray, cv2.CV_64F).var()) < MIN_BLUR_VARIANCE:
        issues.append("Image is blurred; hold still")
    return tuple(issues)


def employee_rows(encodings_path, employees_path):
    """Enrollment table: label, permanent ID and sample count per label."""
    data = read_encodings(encodings_path)
    records = load_employee_map(employees_path)
    rows = []
    for name, samples in data.items():
        employee = records.get(name)
        rows.append({"name": name,
                     "employee_id": employee.employee_id if employee else legacy_employee_id(name),
                     "employee_name": employee.name if employee else name,
                     "mapped": employee is not None,
                     "samples": len(samples)})
    return sorted(rows, key=lambda row: (row["employee_name"].casefold(), row["name"].casefold()))

class EnrollmentService:
    """Validates and appends samples for the catalog at the configured paths."""

    def __init__(self, encodings_path, employees_path, backend=None,
                 min_face_height=MIN_FACE_HEIGHT, check_quality=True):
        self.encodings_path = Path(encodings_path)
        self.employees_path = Path(employees_path)
        self.backend = backend
        self.min_face_height = min_face_height
        self.check_quality = check_quality

    def _detector(self):
        if self.backend is None:
            from .face_backend import OpenCVFaceBackend
            self.backend = OpenCVFaceBackend()
        return self.backend

    def _prepare(self, image, check_quality):
        """(status, encoding-or-message, issues) for a single sample."""
        backend = self._detector()
        try:
            boxes = detect_faces(backend, image)
        except Exception as exc:  # A broken backend must not look like "no face".
            return "error", f"Face detection failed: {exc}", ()
        if not boxes:
            return "no_face", "No face detected. Improve the lighting and face the camera.", ()
        if len(boxes) > 1:
            return "multiple_faces", (f"Found {len(boxes)} faces. Enroll one employee per sample "
                                      "to avoid mis-enrollment."), ()
        if check_quality:
            problems = frame_quality(image, boxes[0], self.min_face_height)
            if problems:
                return "low_quality", "; ".join(problems), problems
        try:
            return "ok", encode_faces(backend, image, boxes)[0], ()
        except Exception as exc:
            return "error", f"Face encoding failed: {exc}", ()

    def _check_name(self, name, employee_id):
        records = load_employee_map(self.employees_path)
        previous = records.get(name)
        if employee_id and previous and previous.employee_id != employee_id:
            return "mapped_conflict", (f"{name!r} is already mapped to {previous.employee_id}; "
                                      "changing an assigned ID needs a deliberate migration"), previous
        return None, "", previous

    def enroll(self, image, name, employee_id=None, check_quality=None):
        return self.enroll_many([] if image is None else [image], name, employee_id, check_quality)

    def inspect(self, image, check_quality=None):
        """Validate one prospective sample without writing anything.

        Returns ``(status, message, issues)``; ``status == "ok"`` means the
        sample would be accepted by :meth:`enroll`.
        """
        check_quality = self.check_quality if check_quality is None else check_quality
        try:
            status, payload, issues = self._prepare(image, check_quality)
        except Exception as exc:
            return "error", str(exc), ()
        return status, ("" if status == "ok" else payload), (() if status == "ok" else issues)

    def employees(self):
        return employee_rows(self.encodings_path, self.employees_path)
    def enroll_many(self, images, name, employee_id=None, check_quality=None):
        name = str(name or "").strip()
        employee_id = str(employee_id or "").strip()
        if not name:
            return EnrollmentOutcome("invalid_name", "Enter the employee name")
        images = [image for image in images if image is not None]
        if not images:
            return EnrollmentOutcome("no_face", "No captured sample to enroll", name=name)
        check_quality = self.check_quality if check_quality is None else check_quality
        try:
            conflict, message, previous = self._check_name(name, employee_id)
            if conflict:
                return EnrollmentOutcome(conflict, message, name=name, employee_id=employee_id)

            outcomes = [self._prepare(image, check_quality) for image in images]
            encodings = [payload for status, payload, _ in outcomes if status == "ok"]
            rejected = [f"sample {index}: {payload}" for index, (status, payload, _) in
                        enumerate(outcomes, start=1) if status != "ok"]
            if not encodings:
                status, payload, sample_issues = outcomes[0]
                return EnrollmentOutcome(status, payload, name=name, employee_id=employee_id,
                                         issues=tuple(sample_issues))
            data = read_encodings(self.encodings_path)
            data.setdefault(name, [])
            data[name].extend(encodings)
            if employee_id:
                canonical = write_employee(self.employees_path, name, employee_id)
            else:
                canonical = previous.name if previous else name
            write_encodings(self.encodings_path, data)
        except (ValueError, OSError, pickle.UnpicklingError, EOFError, AttributeError) as exc:
            return EnrollmentOutcome("error", str(exc), name=name, employee_id=employee_id)

        final_id = employee_id or (previous.employee_id if previous else legacy_employee_id(name))
        message = f"Enrolled {canonical}: {len(encodings)} sample(s) added"
        if rejected:
            message += ". Rejected " + "; ".join(rejected)
        return EnrollmentOutcome("enrolled", message, name=name, employee_id=final_id,
                                 employee_name=canonical, samples_added=len(encodings),
                                 total_samples=len(data[name]), total_people=len(data),
                                 issues=tuple(rejected))

    def update_employee(self, name, display_name):
        """Edit the display name while retaining enrollment labels and permanent IDs.

        Aliases for the same ID receive the same display name. Historical
        attendance rows are never rewritten.
        """
        display_name = str(display_name or "").strip()
        if not display_name:
            raise ValueError("Enter the employee display name")
        data = read_encodings(self.encodings_path)
        if name not in data:
            raise ValueError("Employee enrollment no longer exists; refresh the list")
        records = load_employee_map(self.employees_path)
        employee_id = records[name].employee_id if name in records else legacy_employee_id(name)
        updated = {key: {"employee_id": record.employee_id,
                         "name": display_name if record.employee_id == employee_id else record.name}
                   for key, record in records.items()}
        updated[name] = {"employee_id": employee_id, "name": display_name}
        _atomic_write(self.employees_path,
                      lambda handle: json.dump(updated, handle, ensure_ascii=False, indent=2),
                      binary=False)
        return employee_id

    def delete_employee(self, name):
        """Remove a label from the pickle and the mapping; attendance history is kept."""
        data = read_encodings(self.encodings_path)
        if name not in data:
            raise ValueError(f"{name!r} is not enrolled")
        removed = len(data.pop(name))
        write_encodings(self.encodings_path, data)
        records = load_employee_map(self.employees_path)
        if name in records:
            remaining = {key: {"employee_id": value.employee_id, "name": value.name}
                         for key, value in records.items() if key != name}
            _atomic_write(self.employees_path,
                          lambda handle: json.dump(remaining, handle, ensure_ascii=False, indent=2),
                          binary=False)
        return removed

    def delete_sample(self, name, index):
        data = read_encodings(self.encodings_path)
        samples = data.get(name)
        if not samples:
            raise ValueError(f"{name!r} has no samples")
        if not 0 <= index < len(samples):
            raise ValueError("Sample index out of range")
        samples.pop(index)
        if not samples:
            return self.delete_employee(name)
        write_encodings(self.encodings_path, data)
        return 1
