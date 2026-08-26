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
"""

import argparse
import os
import pickle

import cv2
import face_recognition


ENCODINGS_PATH = "encodings.pickle"


def load_encodings():
    if os.path.exists(ENCODINGS_PATH):
        with open(ENCODINGS_PATH, "rb") as f:
            return pickle.load(f)
    return {}


def save_encodings(data):
    with open(ENCODINGS_PATH, "wb") as f:
        pickle.dump(data, f)


def capture_from_webcam():
    """Opens webcam, waits for SPACE to capture a frame, ESC/Q to cancel."""
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        raise RuntimeError("Could not open webcam.")

    print("Press SPACE to capture the photo, Q to cancel.")
    captured = None
    while True:
        ret, frame = cap.read()
        if not ret:
            continue
        cv2.imshow("Enroll - press SPACE to capture", frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord(" "):
            captured = frame
            break
        elif key == ord("q") or key == 27:
            break

    cap.release()
    cv2.destroyAllWindows()
    return captured


def enroll(name: str, image_path: str = None):
    if image_path:
        image = cv2.imread(image_path)
        if image is None:
            raise FileNotFoundError(f"Could not read image: {image_path}")
    else:
        image = capture_from_webcam()
        if image is None:
            print("Enrollment cancelled.")
            return

    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    boxes = face_recognition.face_locations(rgb, model="hog")

    if len(boxes) == 0:
        print("No face detected in the image. Try again with better lighting/angle.")
        return
    if len(boxes) > 1:
        print(f"Warning: {len(boxes)} faces detected. Using the first one found.")

    encodings = face_recognition.face_encodings(rgb, boxes)
    new_encoding = encodings[0]

    data = load_encodings()
    data.setdefault(name, [])
    data[name].append(new_encoding)
    save_encodings(data)

    print(f"Enrolled '{name}'. Total samples for this person: {len(data[name])}")
    print(f"Total enrolled people: {len(data)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enroll an employee's face.")
    parser.add_argument("--name", required=True, help="Employee full name / ID")
    parser.add_argument("--image", default=None, help="Path to a photo (optional; else uses webcam)")
    args = parser.parse_args()

    enroll(args.name, args.image)
