"""Upload rejection covers another employee's profile and every angle face."""
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from face_attendance import identity_check as checks
from face_attendance.hrm_sync import HRMError


def image():
    return cv2.imencode(".jpg", np.zeros((100, 100, 3), dtype=np.uint8))[1].tobytes()


@pytest.mark.parametrize("label", ["profile photo", "straight angle", "blink angle",
                                  "left angle", "right angle", "up angle", "down angle"])
def test_rejects_another_employee_face_in_every_reference_type(label):
    candidate = np.array([1.0, 0.0])
    with patch.object(checks.angle_templates, "encode_angle", return_value=(candidate, "")), \
            patch.object(checks, "detect_faces", return_value=[(0, 100, 100, 0)]), \
            patch.object(checks, "encode_faces", return_value=[candidate]):
        with pytest.raises(HRMError, match="matches employee EMP2"):
            checks.validate_identity(image(), "EMP1", [("EMP2", label, image())], backend=object())


def test_multiple_face_reference_checks_each_face():
    candidate = np.array([1.0, 0.0])
    with patch.object(checks.angle_templates, "encode_angle", return_value=(candidate, "")), \
            patch.object(checks, "detect_faces", return_value=[(0, 50, 50, 0), (50, 100, 100, 50)]), \
            patch.object(checks, "encode_faces", return_value=[np.array([0.0, 1.0]), candidate]):
        with pytest.raises(HRMError, match="matches employee EMP2"):
            checks.validate_identity(image(), "EMP1", [("EMP2", "left angle", image())], backend=object())


def test_allows_own_face_and_distinct_other_person():
    with patch.object(checks.angle_templates, "encode_angle", return_value=(np.array([1.0, 0.0]), "")), \
            patch.object(checks, "detect_faces", return_value=[(0, 100, 100, 0)]), \
            patch.object(checks, "encode_faces", return_value=[np.array([0.0, 1.0])]):
        checks.validate_identity(image(), "emp1",
                                 [("EMP1", "profile photo", image()), ("EMP2", "left angle", image())],
                                 backend=object())


def test_unreadable_reference_blocks_upload():
    with patch.object(checks.angle_templates, "encode_angle", return_value=(np.array([1.0, 0.0]), "")):
        with pytest.raises(HRMError, match="Cannot verify"):
            checks.validate_identity(image(), "EMP1", [("EMP2", "profile photo", b"broken")], backend=object())


def test_candidate_must_contain_one_face():
    with patch.object(checks.angle_templates, "encode_angle", return_value=(None, "More than one face")):
        with pytest.raises(HRMError, match="one person"):
            checks.validate_identity(image(), "EMP1", [], backend=object())
