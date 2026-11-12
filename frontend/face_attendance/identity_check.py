"""Reject face uploads assigned to another employee before sending them to HRM."""
import cv2
import numpy as np

from . import angle_checks, angle_templates, hrm_sync
from .enrollment import detect_faces, encode_faces


def validate_identity(content, employee_code, references, backend=None):
    """Check one upload against (employee code, photo label, image bytes) references.

    References can contain multiple faces, so every detected face is compared.
    An unreadable reference or a failed model prevents an unchecked upload.
    """
    backend = backend or angle_templates._new_backend()
    candidate, problem = angle_templates.encode_angle(backend, content)
    if candidate is None:
        raise hrm_sync.HRMError(problem + ". Upload a clear photo of one person.")
    owner = str(employee_code).strip().casefold()
    for code, label, image_bytes in references:
        if str(code).strip().casefold() == owner:
            continue
        image = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise hrm_sync.HRMError(f"Cannot verify the {label} for employee {code}. Repair that image first.")
        boxes = detect_faces(backend, image)
        for vector in encode_faces(backend, image, boxes) if boxes else ():
            if angle_checks.same_face(candidate, vector):
                raise hrm_sync.HRMError(
                    f"This face matches employee {code}'s {label}. It cannot be assigned to {employee_code}.")


def validate_hrm_upload(config, employee_code, content, opener=None, backend=None):
    """Read all employee profiles and angles, including inactive employees."""
    def references():
        employees = hrm_sync.fetch_all_employees(config, status="ALL", opener=opener)
        for employee in employees:
            code = str(employee.get("employeeCode") or "").strip()
            if not code:
                raise hrm_sync.HRMError("An employee has no code. The face duplicate check could not finish.")
            if code.casefold() == str(employee_code).strip().casefold():
                continue
            profile = hrm_sync.employee_photo(config, code, opener=opener)
            if profile:
                yield code, "profile photo", profile
            for angle in hrm_sync.list_angles(config, code, opener=opener).get("angles", []):
                key = angle["angle"]
                yield code, f"{key} angle", hrm_sync.angle_image(config, code, key, opener=opener)

    validate_identity(content, employee_code, references(), backend=backend)
