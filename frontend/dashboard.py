"""Run the admin dashboard: python dashboard.py [--start].

The terminal (``python main.py``) and the CLI enrollment (``python
enroll_faces.py``) keep working unchanged; both use the same data files.
"""
from face_attendance.dashboard.app import main


if __name__ == "__main__":
    raise SystemExit(main())