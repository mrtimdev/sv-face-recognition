# Dashboard UI plan (implemented)

Goal: a PyQt6 admin dashboard for the face-recognition attendance system with:

- Live Monitor: camera preview, engine start/pause/stop, status and health cards.
- Real-time Attendance: live check-in feed, per-face verification state, unknown-face alerts.
- Attendance Report: date/employee filters, paging, summaries, CSV export.
- Employees: enrollment by live capture and by photo upload, with name/ID input.
- Settings: camera (webcam index / IP-RTSP URL / file), recognition, storage and appearance.

Architecture rules kept from the terminal app:

- The dashboard reuses `CameraManager`, `RecognitionService/Worker`, `FaceTracker`,
  `AttendanceService` and `PersistenceWorker` unchanged; the engine loop runs on its
  own thread and publishes through Qt signals only.
- Recognition, DB writes and snapshot I/O never happen on the UI thread.
- `Config` is frozen: settings changes are saved to `settings.json` and applied on
  engine restart.
- Enrollment reuses the engine camera (or a temporary session when it is stopped);
  multi-face images are rejected; writes are atomic.
- Reports use a separate read-only SQLite connection (`PRAGMA query_only=ON`).
- One engine per data directory, enforced by `attendance.lock`.
- RTSP credentials are masked on screen and never logged; `settings.json` is gitignored.

Entry points:

- `python dashboard.py [--start]` or `python -m face_attendance.dashboard`
- The terminal app (`python main.py`) and CLI enrollment (`python enroll_faces.py`)
  keep working unchanged and share the same data files.

Tests: `tests/test_settings.py`, `tests/test_report.py`, `tests/test_dashboard.py`
(bridge conversion, window build/screen switching, headless engine with fake
backend/capture, engine lock) alongside the existing 40 tests.
