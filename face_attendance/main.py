"""Command-line configuration and entry point."""
import argparse
import logging
from dataclasses import replace
from pathlib import Path

from .config import Config, parse_source


def parser():
    p = argparse.ArgumentParser(description="Face ID attendance terminal with automatic verification and capture")
    p.add_argument("--source", default="0", help="Webcam index (0, 1, ...) or an IP/RTSP URL")
    p.add_argument("--tolerance", type=float, help="SFace cosine distance: lower is stricter; default 0.5")
    p.add_argument("--process-every", "--detection-interval", dest="detection_interval", type=int,
                   help="Minimum camera frames between detections; default 3")
    p.add_argument("--recognition-interval", type=int, help="Minimum frames between unconfirmed encodings; default 5")
    p.add_argument("--capture-after", type=float, help="Verified presence before capture; default 3 seconds")
    p.add_argument("--width", type=int, help="Requested camera width; default 1280")
    p.add_argument("--height", type=int, help="Requested camera height; default 720")
    p.add_argument("--fps", type=float, help="Target camera/UI FPS; default 30")
    p.add_argument("--scale", type=float, help="Detector resize factor; default 0.25")
    p.add_argument("--db", type=Path, help="SQLite database path")
    p.add_argument("--captures", type=Path, help="Evidence image directory")
    p.add_argument("--encodings", type=Path, help="Existing trusted encodings.pickle")
    p.add_argument("--employees", type=Path, help="Optional enrollment name -> HRM employee map")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    mapping = {"tolerance": "face_tolerance", "capture_after": "capture_after_sec", "width": "camera_width",
               "height": "camera_height", "fps": "target_fps", "scale": "detection_scale", "db": "db_path",
               "captures": "capture_dir", "encodings": "encodings_path", "employees": "employees_path"}
    values = {mapping.get(key, key): value for key, value in vars(args).items() if value is not None}
    values["source"] = parse_source(args.source)
    try:
        config = replace(Config(), **values)
    except ValueError as exc:
        p.error(str(exc))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    from .runtime import AttendanceApplication
    try:
        AttendanceApplication(config).run()
    except KeyboardInterrupt:
        pass
    except (FileNotFoundError, ValueError) as exc:
        p.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
