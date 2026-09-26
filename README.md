# Face ID attendance terminal

A local OpenCV attendance terminal using **YuNet** face detection, **SFace**
identity matching, **MediaPipe Face Mesh** eye/mouth tracking, and mandatory
**MiniFASNet** presentation checks. The live camera keeps moving during
recognition, snapshot writing, SQLite commits and audio playback. Neither dlib
nor the `face_recognition` package is required. Models and licenses are bundled
in `face_attendance/assets`; inference does not upload faces or download models.

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

Audio uses the existing `alert.wav`. macOS uses its native `afplay` and Windows
uses built-in `winsound`; neither needs `simpleaudio`. On Linux, install the
optional backend below or use a system `paplay`/`aplay` player:

```bash
pip install -r requirements-audio.txt
python generate_alert_sound.py
```

The application works without audio. Sound plays asynchronously with a cooldown
and no overlapping playback from the same player. Use **Test alert sound** in
Live Monitor to check playback. Unknown-face alerts use the same backend, and
the dashboard's attendance sound plays after a successful save.


## Dashboard (PyQt6 admin UI)

Launch the graphical admin dashboard:

```bash
python dashboard.py            # window opens with the engine stopped
python dashboard.py --start    # open the camera and record attendance immediately
python -m face_attendance.dashboard
```

Uncaught dashboard callback errors are written to `logs/dashboard-errors.log`
(rotated locally, with no upload). The window stays open, recording pauses,
and an error dialog offers the details instead of PyQt aborting Python. Fully
restart the dashboard after an application error. If it repeats, use the Python
traceback in that log to diagnose the failing callback; macOS's native crash
report alone does not include that traceback.

Attendance capture triggers a 720ms cyan-white screen flash with expanding
shutter corners and a fading capture badge. Simultaneous captures share one
pulse; the overlay leaves controls clickable and does not alter saved photos.
Try it without recording using **Live Monitor → More → Preview capture flash**.

Screens:

- **Live Monitor** - live camera preview with overlays, engine start/pause/stop,
  and health cards (camera status, FPS, latency, storage queue, DB path).
- **Real-time Attendance** - streaming check-in feed for today with snapshot
  thumbnails, per-face verification state/progress and unknown-face alerts.
- **Attendance Report** - date presets/custom range, employee search, paging,
  summary cards, outbox pending/synced counts and CSV export (UTF-8 BOM).
- **Enrolled Employees** - a searchable photo table with enrollment-status
  filtering and row actions: **Edit**, **Update photo**, and **Delete**.
  **Add employee** opens a focused live-capture/photo-upload form with fixed
  Save/Close controls. Name edits keep permanent IDs and historical attendance;
  aliases for the same ID share the updated display name. Photo updates add face
  samples and replace the directory image. Deletion requires confirmation and
  removes the selected enrollment and its saved photo, keeping attendance history.
  Missing enrollment photos use initials; attendance captures are never presented
  as enrollment photos. Double-click a row for its profile and capture gallery.
- **Settings** - camera source (webcam index, IP/RTSP URL with masked password,
  video file), resolution/FPS, recognition tuning, storage paths and dark/light
  theme. Changes are saved to `settings.json` and applied when the engine
  restarts (`Config` is immutable while running).

Behavior notes:

- One engine per data directory (`attendance.lock`); a second dashboard or a
  terminal instance refuses to run against the same data.
- Reports read through a separate read-only SQLite connection; they can never
  record or modify attendance.
- Live Monitor has four mutually exclusive attendance checkboxes: **Only Face
  for attendance**, **Face with Blink**, **Face with Smile**, and **Face with
  Blink and Smile**. Choose one and click **Apply requirements** to save it;
  a running engine restarts and clears previous verification evidence. The
  default is **Face with Blink**, including settings files without a mode.
  Only Face skips expression/landmark processing; face recognition, verified
  presence, cooldown and the separate anti-spoof checks still apply. Both-action
  mode accepts either order and prompts for the remaining action.
  A pending selection is labelled until applied, so it is clear which mode
  the running engine is using.
- For expression modes, look at the camera with a relaxed face briefly, then
  follow the blink/smile prompt. No head turns are required.
  Eye thresholds adapt separately to each eye's normal opening. A smile must
  widen and lift the mouth corners relative to the initial expression for a
  short hold; simply opening the mouth or showing a static smiling photo is
  insufficient. If already smiling on arrival, relax briefly and smile again,
  or use the blink option.
- Full-resolution landmarks are sampled on every detection. Brief optical-flow
  failures up to 0.75 seconds freeze attendance time without counting the
  interruption; longer failures clear it. Capture requires recovered flow
  and fresh verification evidence. Short missing-landmark gaps retain
  calibration but break an incomplete blink/smile. Identity changes, overlap,
  disconnects and sampling gaps over 0.75 seconds reset verification. Completed
  passes expire after 10 seconds and missing face evidence revokes them.
  Blink calibration uses a median to resist single eyelid outliers. Expression
  prompts appear while the anti-spoof dwell runs, and recorded/cooldown feedback
  stays visible after a successful save. Low-confidence model results ask for
  a front-facing view in even light; they still block attendance.
  Anti-spoof crops keep the full available surrounding area at frame edges.
  If a recognized face stays blocked, the Live Monitor warning identifies the
  liveness gate; its tooltip and System Logs show both model scores. Camera
  effects such as Portrait/background blur or beauty filters should be turned
  off when diagnosing a rejection. A Snapshot preserves a clean frame for the
  offline `scripts/check_anti_spoof.py` diagnostic.
- A **separate mandatory anti-spoof check** now runs before recording. Two
  bundled MiniFASNet models inspect the face and its surrounding image, even
  when identity encoding is reused. **Both must score at least 0.90 on every
  sample**, with at least six consecutive samples spanning the full attendance
  dwell time (minimum 1.2 seconds; normally 3 seconds). This stricter threshold
  is an application policy, not a measured accuracy figure. Every rejection
  clears accumulated presence and blink/smile evidence, including frames that
  reuse an identity encoding. A blink/smile cannot override a rejection.
  Missing/corrupt models, inference failures, small/clipped faces, close-ups
  without enough surrounding context and stale results block attendance.
  The saved snapshot is the actual frame checked by the models, rather than a
  newer, unexamined preview frame.
  Model failures never fall back to expression-only recording. Models run
  locally through existing OpenCV; no images are uploaded or downloaded at runtime.
- The UI shows **Photo/video suspected** or a model/quality error when blocked.
  The models are bundled in source and packaged builds, with hashes and license
  in `face_attendance/assets/anti_spoof/NOTICE.md`. Rebuild an existing packaged
  app to include the new code and model assets.
- This is probabilistic RGB presentation-attack detection, not a guarantee or
  certification. Replays can still evade it and real faces can be rejected.
  Test real users and actual phone/paper presentations using the deployment
  camera; an original selfie file is not a test of camera-captured phone replay.
  A video injected directly into the camera feed is outside this check's scope.
- The report can optionally label rows Late/On time against a configured work
  start time (computed, never stored); no shift/absent policy exists.
- RTSP passwords are masked on screen and never logged. `settings.json` and
  `attendance.lock` are gitignored local state.
- macOS shows a camera-permission prompt on first run.


## Enrollment and employee IDs

The default catalog is now `encodings_sface.pickle`, with a schema version and
SFace model identifier around the sample dictionary. **Old dlib embeddings are
incompatible**, despite also having 128 values, and are rejected at load time.
They cannot be converted mathematically: re-encode the original enrollment
photos or re-enroll the employee. Keep `encodings.pickle` as a backup.

For an existing installation, stop the app and run this once before restarting:

```bash
python scripts/migrate_sface.py
# Custom paths:
python scripts/migrate_sface.py --legacy staff.pickle --output staff_sface.pickle --employees employees.json
```

Migration uses only exact, uniquely named `enrollment_photos/{safe_label}.jpg`
files saved by the dashboard. Missing, ambiguous, multiple-face or low-quality
photos produce an empty label that requires re-enrollment. It refuses to replace
an existing destination. It never guesses identities from attendance captures
and never changes employee mappings or attendance history. Review its report,
then re-enroll labels with zero samples in Enrolled Employees. If this installation has
already been migrated, do not rerun the command against the same output file.

Legacy dashboard settings automatically select the corresponding `_sface`
catalog and reset distance settings for the new model; saving Settings persists
the backend marker. Custom CLI paths must point to the new catalog.
`--tolerance` now means **cosine distance** (`1 - cosine similarity`), default
0.50 with a 0.035 margin over a competing employee. Lower is stricter. These
are conservative application defaults, not measured accuracy guarantees; validate
with enrolled users and lookalikes on the deployment camera.

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
| `catalog.py`, `recognition.py` | Versioned enrollment IDs, YuNet, SFace and selective matching |
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
| `detection_scale` | 0.25 | YuNet input size relative to camera frame |
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
reconnect behavior and blink/smile detection on the target camera. Test a live person,
a static paper photo, a phone photo (stationary and moving), a blinking video,
a video with head/mouth movements, covered eyes,
leaving/re-entering, identity swaps, and disconnect/reconnect. Static photos and missing evidence must create no attendance rows.
Blink/smile alone must never override a failing anti-spoof result. Synthetic tests verify the
software gates; they do not measure real-world spoof rejection. For broader
attack coverage, use independently evaluated PAD/depth/IR equipment; see
[NIST's PAD evaluation](https://www.nist.gov/news-events/news/2023/09/whats-wrong-picture-nist-face-analysis-program-helps-find-answers).
The OpenCV font used
by the existing app renders ASCII labels; full Unicode display names require
a font-rendering extension.

### Checking a recorded presentation without writing attendance

```bash
venv/bin/python scripts/check_anti_spoof.py /path/to/printed-photo-test.mov --expected attack --report /tmp/print-report.json
venv/bin/python scripts/check_anti_spoof.py /path/to/genuine-person.mov --expected live --report /tmp/live-report.json
```

This local diagnostic retains original camera pixels and matches the default
dashboard detection scale. It prints each model's live/attack scores, rejected
samples and sustained passing windows as JSON. Match customized settings with
`--detection-scale` and `--presence-sec`. Images are evaluated once. For an
attack recording, any passing sample fails the expected check (exit code 2);
missing detections or model errors must be reviewed as inconclusive evidence.
For a live video, at least one sustained passing window is required.
It never opens the attendance database, enrolls faces, saves images or sends
notifications. Record the **attendance camera seeing the printed photo or
phone playing a video**, plus a separate genuine-person recording. A source
selfie clip lacks the print/display artifacts needed to evaluate a replay.

The real-model regression tests include the upstream live, printed-photo and
screen examples, with provenance in `tests/fixtures/anti_spoof/NOTICE.md`.
Passing these three examples does not validate arbitrary prints, video replays,
paper masks or 3D masks. The implementation follows [UniFace's MiniFASNet
documentation](https://yakhyo.github.io/uniface/modules/spoofing/) and centered,
clipped crops; it retains the existing local OpenCV runtime and bundled weights.
