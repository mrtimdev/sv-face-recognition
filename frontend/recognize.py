"""Compatible entry point for the refactored Face ID attendance terminal.

All existing command-line options remain supported. Settings now live in
face_attendance/config.py; python main.py is an equivalent entry point.
"""
from dataclasses import replace

from face_attendance.config import Config, parse_source
from face_attendance.main import main


def run(source=None, tolerance=0.5, process_every=3, capture_after=3.0):
    """Keep existing scripts that import recognize.run working."""
    from face_attendance.runtime import AttendanceApplication
    config = replace(Config(), source=parse_source(source), face_tolerance=tolerance,
                     detection_interval=process_every, capture_after_sec=capture_after)
    AttendanceApplication(config).run()


if __name__ == "__main__":
    main()
