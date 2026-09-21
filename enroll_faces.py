"""
enroll_faces.py

Registers a new employee's face into the local encodings database.

Usage:
    python enroll_faces.py --name "John Doe"
    python enroll_faces.py --name "John Doe" --image path/to/photo.jpg

If --image is not provided, it opens your webcam and lets you capture
a photo by pressing SPACE (press Q to cancel).

Encodings are stored in encodings.pickle as:
    { "John Doe": [encoding1, encoding2, ...], ... }

You can enroll the same person multiple times (different angles/lighting)
to improve recognition accuracy -- each new encoding is appended.

The atomic writers and the optional quality gates live in
``face_attendance.enrollment`` and are shared with the dashboard, so both entry
points write identical formats.
"""

import argparse

import cv2
import face_recognition

from face_attendance import enrollment
from face_attendance.catalog import load_employee_map
from face_attendance.config import Config, parse_source


# Kept at module level so existing callers can redirect the catalog paths and
# keep the atomic writers patchable.
ENCODINGS_PATH = str(Config().encodings_path)
EMPLOYEES_PATH = Config().employees_path


def load_encodings():
    """The existing local format: {enrollment_name: [encoding, ...]}."""
    return enrollment.read_encodings(ENCODINGS_PATH)


def save_encodings(data):
    enrollment.write_encodings(ENCODINGS_PATH, data)


def save_employee(name, employee_id):
    return enrollment.write_employee(EMPLOYEES_PATH, name, employee_id)


def capture_from_webcam(source=0):
    """Opens webcam, waits for SPACE to capture a frame, ESC/Q to cancel."""
    cap = cv2.VideoCapture(parse_source(source))
    if not cap.isOpened():
        raise RuntimeError("Could not open webcam.")

    print("Press SPACE to capture the photo, Q to cancel.")
    captured = None
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("Camera disconnected. Please reconnect and retry enrollment.")
                break
            cv2.imshow("Enroll - press SPACE to capture", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord(" "):
                captured = frame
                break
            elif key == ord("q") or key == 27:
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
    return captured


def enroll(name: str, image_path: str = None, employee_id=None, source=0, check_quality=False):
    if not name.strip() or (employee_id is not None and not employee_id.strip()):
        raise ValueError("Employee name and supplied employee ID must not be empty")
    if employee_id:
        previous = load_employee_map(EMPLOYEES_PATH).get(name)
        if previous and previous.employee_id != employee_id:
            raise ValueError(f"{name!r} is already mapped to {previous.employee_id}")
    if image_path:
        image = cv2.imread(image_path)
        if image is None:
            raise FileNotFoundError(f"Could not read image: {image_path}")
    else:
        image = capture_from_webcam(source)
        if image is None:
            print("Enrollment cancelled.")
            return

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    boxes = face_recognition.face_locations(rgb, model="hog")

    if len(boxes) == 0:
        print("No face detected in the image. Try again with better lighting/angle.")
        return
    if len(boxes) > 1:
        print(f"Found {len(boxes)} faces. Use a photo with exactly one employee to avoid mis-enrollment.")
        return
    if check_quality:
        # Off by default so previously accepted photos keep working.
        issues = enrollment.frame_quality(image, boxes[0])
        if issues:
            print("Sample rejected: " + "; ".join(issues))
            return

    encodings = face_recognition.face_encodings(rgb, boxes)
    new_encoding = encodings[0]

    data = load_encodings()
    data.setdefault(name, [])
    data[name].append(new_encoding)
    if employee_id:
        save_employee(name, employee_id)
    save_encodings(data)

    print(f"Enrolled '{name}'. Total samples for this person: {len(data[name])}")
    print(f"Total enrolled people: {len(data)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enroll an employee's face.")
    parser.add_argument("--name", required=True, help="Employee full name / ID")
    parser.add_argument("--image", default=None, help="Path to a photo (optional; else uses webcam)")
    parser.add_argument("--employee-id", help="Stable employee ID from your HRM backend (optional)")
    parser.add_argument("--source", default="0", help="Webcam index for enrollment (default 0)")
    parser.add_argument("--check-quality", action="store_true",
                        help="Also reject tiny, blurred or badly lit samples")
    args = parser.parse_args()

    enroll(args.name, args.image, args.employee_id, args.source, args.check_quality)
