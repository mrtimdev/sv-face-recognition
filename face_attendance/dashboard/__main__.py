"""Entry point: ``python -m face_attendance.dashboard [--settings path] [--start]``."""
from .app import main


if __name__ == "__main__":
    raise SystemExit(main())
