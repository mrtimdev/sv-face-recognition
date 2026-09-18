# Face ID attendance terminal

The existing `face_recognition`/HOG pipeline and enrollment format now run in a
modular attendance terminal. The live camera keeps moving during recognition,
snapshot writing, SQLite commits and audio playback.

The UI retains green/red L-shaped brackets, live duration, an animated scan
line, FPS, employee/active-face counts and a live clock. Verification progress
replaces the old 60-second fill bar. A clean capture produces a brief flash and
shutter, then a centered, fading success card **after SQLite commits**. Each
employee has an independent 30-second cooldown.

## Run

Use the existing virtual environment, or install the core requirements:

```bash
source venv/bin/activate
pip install -r requirements.txt
python main.py --source 0
```

`python recognize.py` and the existing `--tolerance`, `--process-every` and
`--capture-after` options still work. Press **Q** or **Esc**, or close the window,
to stop. Accepted attendance jobs finish before the database closes.

```bash
python main.py --source 1
python main.py --source "rtsp://user:pass@camera:554/stream1"
python recognize.py --tolerance 0.45 --process-every 2 --capture-after 3
python main.py --width 640 --height 480 --fps 30 --scale 0.25
python main.py --width 1920 --height 1080
python main.py --help
```

Default data paths are relative to the project directory, so launching from a
different working directory does not create a second attendance database.
Override paths with `--db`, `--captures`, `--encodings`, and `--employees`.

The HUD is appended beside the full camera image, so employees at the right
edge are still visible. Its width and typography scale with resolution.
Camera sizes/FPS are requests to the driver, not guaranteed capabilities.

Optional audio uses the existing `alert.wav`:

```bash
pip install -r requirements-audio.txt
python generate_alert_sound.py
```

The application works without audio. Sound is cached and played asynchronously
from the persistence worker, with an alert cooldown.

## Enrollment and employee IDs

Existing `encodings.pickle` files work unchanged. The file still stores
`{enrollment_name: [128-dimensional encoding, ...]}`.

```bash
python enroll_faces.py --name "Tim Dev" --image faces/tim-dev.png
python enroll_faces.py --name "New Employee" --employee-id "EMP-125"
python enroll_faces.py --name "New Employee" --employee-id "EMP-125" --source 1
```

Enrollment appends samples atomically. Images containing multiple faces are
rejected to avoid enrolling the wrong person. Use a few clear, single-person
photos at different angles.

An optional local `employees.json` maps enrollment labels to permanent HRM IDs
and display names; see [employees.example.json](employees.example.json):

```json
{
  "Enrollment label": {
    "employee_id": "EMP-125",
    "name": "Employee display name"
  }
}
```

Aliases can share the same employee ID. Without a mapping, the application
derives a deterministic `local-<uuid>` ID from the enrollment label. Exact
duplicate encodings assigned to different employees generate a startup warning;
ambiguous matches cannot record attendance. Do not loosen tolerance to resolve
duplicate enrollment data.

Adding an explicit mapping later migrates attendance using the old derived
local ID, carries cooldowns forward, and updates unsent outbox events. Historical
display names remain intact. Changing one already-assigned HRM ID to another
requires a deliberate data migration. Restart the terminal after enrollment or
mapping changes.

Only load trusted pickle files. Employee mappings, face encodings, captures and
the attendance database are local data and are ignored by Git. Back them up
together.

## Architecture

```text
Camera thread -> newest clean frame ----------------------> UI thread
                      |
                 one-slot request
                      v
               Recognition thread -> one-slot result -> FaceTracker
                                                          |
                                                   AttendanceService
                                                          |
                                                    bounded job queue
                                                          v
               Persistence thread: snapshot + SQLite transaction + outbox
                                                          |
                                                   commit/error result
                                                          v
                                                UI state and animation
```

There are three long-running application workers plus the main GUI thread.
Frames and pending recognition results never accumulate. The GUI owns tracks
and state; cross-thread messages contain frozen metadata and clean image
references. Only the persistence thread owns its SQLite connection.

| Module | Responsibility |
| --- | --- |
| `config.py` | Defaults, validation and data paths |
| `models.py` | Face tracks, explicit states and thread messages |
| `camera.py`, `channels.py` | Capture/reconnects and one-slot frame mailboxes |
| `catalog.py`, `recognition.py` | Enrollment IDs, HOG, selective encoding and matching |
| `tracking.py`, `geometry.py` | Sparse optical flow and one-to-one detection association |
| `attendance.py` | Freshness, verification, pending jobs and in-memory cooldowns |
| `repository.py`, `storage.py`, `persistence.py` | Transactions, clean evidence, CSV and audio |
| `ui.py` | Responsive corner brackets, HUD and timestamp-based animations |
| `runtime.py`, `main.py` | Lifecycle, component coordination and CLI |

The recognition backend computes distances once per encoding, then compares the
best samples for each **employee**, with a configurable ambiguity margin.
It reuses confirmed identity between checks, but re-encodes near capture.
Optical flow updates boxes on each new preview frame. Detector results are
associated against historical positions to account for worker latency.
Overlaps, conflicting matches, missing faces and stale results revoke
verification. This favors restarting verification over recording the wrong
employee during a crossing/occlusion.

## State and attendance rules

```text
DETECTING -> RECOGNIZING -> VERIFYING -> CONFIRMED -> CAPTURING
                                                        |
                          commit -> SUCCESS -> COOLDOWN -> READY -> VERIFYING
                          failure -> ERROR -> fresh verification -> retry

Unmatched face -> UNKNOWN -> automatic reset/retry
Lost/disconnected/ambiguous face -> verification reset
```

Auto-capture requires a known employee, repeated identity confirmations,
approximately three seconds of continuous verified presence, a recent encoding,
recent detection and valid tracking, a live camera, no pending job, and no
employee cooldown. The UI interpolates progress; elapsed display time alone
cannot authorize attendance. A departure resets verification, including a
departure shorter than the visual track timeout.

The snapshot is a copy of the clean camera frame taken before rendering.
The storage queue is bounded; a full queue shows an error/retry state without
blocking the preview. The database is the final eligibility authority, so
another process using the same SQLite file cannot bypass the cooldown.
Shift/check-in/check-out policies are not inferred; the retained attendance
status is `KNOWN`, with repeat attendance permitted after cooldown and new
verification.

## Persistence and Spring Boot integration

The original `attendance` rows and columns are preserved. Startup adds
`employee_id`, `event_id`, UTC/epoch timestamps and indexes, plus
`attendance_cooldowns` and `attendance_outbox`. The old name-based cooldown
table is retained and imported. Naive timestamps from the old application are
interpreted in the machine's local timezone.

A `BEGIN IMMEDIATE` transaction checks employee eligibility, writes a unique
snapshot, and commits attendance, cooldown and an outbox event together.
`event_id` is unique for idempotent retries. SQLite uses WAL and a bounded busy
timeout. Snapshot errors or database errors cannot produce a success message.
Rollback removes the snapshot created by that failed job; a process/power loss
between file creation and commit can leave an unreferenced JPEG, never a falsely
committed attendance row.

A committed outbox event contains:

```json
{
  "eventId": "capture UUID",
  "employeeId": "EMP-125",
  "employeeName": "Employee display name",
  "capturedAt": "2026-09-18T13:35:24.000+00:00",
  "recordedAt": "2026-09-18T13:35:24.150+00:00",
  "status": "KNOWN",
  "snapshotPath": "/absolute/local/path/to/evidence.jpg",
  "verifiedPresenceSeconds": 3.1
}
```

`AttendanceRepository.pending_outbox(limit)` reads unsent events and
`mark_synced(event_id)` acknowledges one after your backend confirms receipt.
Use a separate synchronizer process/connection, an idempotent Spring Boot
endpoint keyed by `eventId`, and upload the local image separately if needed.
No endpoint, authentication scheme or network requests are invented here.
Local attendance continues independently of future network availability.
Historical pre-refactor rows are not automatically queued for upload.

`attendance_log.csv` remains a throttled **recognition observation log**.
It is not proof that attendance committed; SQLite is the attendance record.

## Performance tuning

All tuning defaults are in [face_attendance/config.py](face_attendance/config.py).
The original 0.25 detector scale and 0.5 tolerance are retained. Detection moves
from every second frame to every third by default; encoding is scheduled
separately.

| Setting | Default | Meaning |
| --- | --- | --- |
| `camera_width / camera_height` | 1280 / 720 | Requested camera resolution |
| `target_fps` | 30 | Camera request and maximum UI refresh rate |
| `detection_scale` | 0.25 | HOG input size relative to camera frame |
| `detection_interval` | 3 | Minimum source-frame gap between detections |
| `recognition_interval` | 5 | Minimum frame gap for unconfirmed/final verification encodings |
| `stable_recheck_sec` | 0.75 | Periodic re-encoding of stable tracks |
| `face_tolerance / identity_margin` | 0.5 / 0.035 | Acceptance threshold / competing-employee separation |
| `min_confirmation_frames` | 3 | Matching encoded observations before identity confirmation |
| `capture_after_sec` | 3 | Required verified presence |
| `cooldown_sec` | 30 | Persistent employee cooldown |
| `detection_fresh_sec / identity_fresh_sec` | 0.8 / 1.5 | Maximum trusted observation ages |
| `max_result_age_sec` | 0.8 | Drop late worker results |
| `session_timeout` | 3 | Retain/fade lost visual tracks |
| `tracker_width` | 480 | Small grayscale input for optical flow |
| `flash_duration / shutter_duration` | 0.22 / 0.42 | Nonblocking capture animation |
| `success_duration` | 3.5 | Success card duration |
| `persistence_queue_size` | 8 | Bounded image jobs |
| `opencv_threads` | 1 | Avoid excessive OpenCV CPU thread competition |

Intervals count captured source frames; if workers fall behind, only the
newest request survives. Camera/UI and recognition FPS are deliberately
different. The HUD reports preview FPS and camera FPS.

For a slower CPU, first request 640x480 or increase `--process-every`.
For smaller/distant faces, increase `--scale` to 0.35 or 0.5 at additional CPU
cost. Validate tolerance/ambiguity margin against your own employees and camera
lighting. Do not assume a higher camera FPS improves recognition.

Network sources use FFmpeg open/read timeouts, configured to 2.5 seconds, and
retry after a one-second interruptible backoff. A one-second frame watchdog
pauses attendance before a blocked read returns. Backend support for timeouts
and buffering varies; see the official
[OpenCV video I/O property documentation](https://docs.opencv.org/4.10.0/d4/d15/group__videoio__flags__base.html).
Some USB drivers do not support cancellable reads; shutdown logs that case
instead of racing `release()` against an active read. Test the actual USB/RTSP
device before deployment.

## Verification

```bash
python -m unittest discover -s tests -v
```

Tests use temporary databases, mocked disconnect/reconnect sources, controlled
recognition observations and synthetic frames. They cover:

- Clean snapshots and success only after commit.
- Multiple employees and duplicate tracks for the same employee.
- Concurrent SQLite writers, event replay, restart cooldowns and alias migration.
- Snapshot/transaction failures and retry eligibility.
- Optical-flow motion, reordered faces, conflicts, overlap, stale results and reconnect generations.
- Full automatic verification/capture and departure/re-entry.
- Bounded latest-frame behavior and graceful persistence shutdown.
- UI states/animations at 640x480, 1280x720 and 1920x1080.
- Existing enrollment format and safe single-face enrollment.

A real webcam/RTSP soak test is still needed to measure sustained FPS and
reconnect behavior on the target camera. Face matching/continuous presence
does not implement anti-spoofing or liveness detection. The OpenCV font used
by the existing app renders ASCII labels; full Unicode display names require
a font-rendering extension.
