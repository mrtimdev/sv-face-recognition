# Face ID attendance terminal

For unattended macOS operation, see [OPERATIONS.md](OPERATIONS.md) and the
[security/reliability review](SECURITY_REVIEW.md). Hardware soak acceptance is
separate from software regression tests.

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

The dashboard includes fourteen distinct local WAV tones for detection, unknown
faces, recognized employees, multiple faces, quality and liveness guidance,
presentation rejection, verification, attendance results, and camera health. No audio download or generation step is
required. Files in `face_attendance/assets/sounds` are included in packaged builds;
`scripts/generate_builtin_sounds.py` reproduces them using the standard library.

Use **Settings → Built-in sounds** to mute sounds or disable detection,
guidance, and unenrolled-face cues. Use the selector beside **Test alert sound**
in Live Monitor to preview each tone (preview works even while alerts are muted).
An existing custom `alert_path` WAV overrides only the attendance-success tone.

Detection is debounced; no-face audio plays once after the view has been empty
for two seconds following a face. It does not repeat on an empty camera or play
on startup. Unenrolled and quality cues require a sustained condition and play
once per tracked visit, with global cooldowns to coalesce groups. Camera loss
has its own tone. Poor-quality faces are not announced as unenrolled. A single
dashboard owner prevents duplicate success audio and overlapping cues; errors
and successful saves take priority over guidance. Preview continues when audio
is unavailable.

macOS uses native `afplay` and Windows uses `winsound`. On Linux, install
`requirements-audio.txt` or provide `paplay`/`aplay`. The terminal workflow retains
its optional `alert.wav` observation/save alert; `generate_alert_sound.py`
creates that legacy file.


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

- **Live Monitor** and **License Plates** can each open in a separate window.
  Use the preview's **Open in new window** button or **More → Open in new window**.
  Both monitor windows can stay open while the dashboard shows reports, employees,
  settings or another page. They share the existing engine and plate worker;
  opening a window never starts a second camera or duplicate attendance pipeline.
  The original tab offers **Show window** and **Return to dashboard**. The return
  button, preview control, or native window close button puts the same page back
  in its tab without stopping recognition. Use the monitor's **Stop** control to
  stop processing. Each external window supports its own full-screen toggle and
  **Escape**, independent of the dashboard. Window size/position is reused while
  this dashboard instance runs. Permissions, theme and settings remain shared;
  sign-out, restart and application shutdown close the external pages too.

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
  Each attendance snapshot is a padded crop of that employee's face from the
  exact frame checked by the models. The source-frame detection box is retained
  separately from the moving preview box. Padding stops before neighboring
  faces, and overlapping face boxes defer capture. Liveness still uses the
  original camera pixels and the models' required context, without matting.
  The full frame is retained in `captures/context/` under the same filename,
  with a JSON companion recording employee/event/track IDs, source sequence and
  generation, and face/crop coordinates. Live activity opens on the face crop;
  **Full frame** in its detail modal shows the original. Report details also
  provide **Open full frame**. Deleting a record's snapshots removes the crop,
  context image, and metadata together. Existing full-frame snapshots remain
  readable without migration.
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

Employee setup accepts up to 64 samples per save (automatic capture stops at 8).
Names and employee IDs are limited to 100 characters. The desktop rejects stale
camera frames and exact repeated captures; clearing, closing or changing setup
invalidates unfinished checks. Capture countdowns wait the selected number of
seconds, and background checks use a snapshot of the current settings.

Every accepted sample must contain one fully visible face and a finite,
nonzero 128-value SFace embedding. Quality checks control the lighting, blur and
minimum face size gates; they never bypass these structural or identity checks.
Samples must match both the first accepted sample in the batch and, when adding
photos, an existing sample for the employee or their alias. The default maximum
cosine distance is 0.5 (`EnrollmentService(identity_distance=...)`); validate this
policy with your deployment images before changing it. Rejected samples are
reported in the save result and do not get saved as enrollment photos.

Detection and encoding share one RGB image and one model lock, avoiding a second
YuNet pass. Service commits within one Python process are serialized and re-read
the latest catalog. If catalog publication fails, the prior employee mapping is
restored and newly created sample photos are removed. Mapping and catalog remain
separate filesystem files: this is recovery from reported write failures, not a
power-loss transaction or coordination between separate API/CLI/dashboard
processes. Keep a single enrollment writer active and back up the files together.

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

New enrollment now checks each face against **other employee IDs** before
writing photos or records. For example, enrolling “Tim Dev 2” with a face
matching “Tim Dev” is refused with the existing employee's name and ID; use
**Update photo** on the original employee instead. A duplicate in any valid
sample rejects the entire save, even when quality checks are turned off.
Additional samples and aliases under the **same employee ID** remain allowed.
The check uses normalized SFace cosine distance and the dashboard's configured
face tolerance (default 0.5). CLI/service callers use the default unless an
explicit `duplicate_distance` is supplied.

Existing records are reviewed asynchronously in **Enrolled Employees**. Use
**Refresh**, select **Possible duplicate faces**, and choose **Edit → Review
match** to inspect the named counterpart. Profiles requiring review are excluded
from the directory's “Ready for attendance” count. No employee is automatically
merged, reassigned or deleted. After confirming which profile is mistaken, use
that profile's existing **Delete employee** action and confirmation; attendance
history is retained. Restart the dashboard after updating the code to load this
behavior.

Imports apply the same cross-employee check, including conflicts between entries
inside one archive. Mobile captures name the conflicting employee; completion
checks the current published catalog again before saving, so old pending
sessions cannot bypass the duplicate gate. These checks compare face templates;
deployment threshold calibration and human review remain necessary for close
matches.

Adding an explicit mapping later migrates attendance using the old derived
local ID, carries cooldowns forward, and updates unsent outbox events. Historical
display names remain intact. Changing one already-assigned HRM ID to another
requires a deliberate data migration. Restart the terminal after enrollment or
mapping changes.

Only load trusted pickle files. Employee mappings, face encodings, captures and
the attendance database are local data and are ignored by Git. Back them up
together.

## Trying liveness and moving employees

Every installer includes these optional tools; none of them changes anything
until used.

- **Live Monitor › More › Liveness test** runs this computer's detector and
  liveness models on three sample photos shipped with the app: a real face, a
  printed photo and a phone screen (Apache-2.0 upstream examples, see
  `face_attendance/assets/samples/NOTICE.md`). The real face must pass and both
  photos must be blocked. No camera or enrolled employees are needed, so it is
  the first check to run on a new installation.
- **Demo employee** (in the same window) enrolls the real-face sample as
  "Demo employee (sample)", ID `DEMO-0001`. Show any sample photo to the camera
  on a phone or on paper: Live Monitor recognizes the demo employee but blocks
  attendance as a photo. Remove it when you're done.
- **Enrolled Employees › More › Export / Import employees** moves enrolled
  employees between computers in one `.zip`: face vectors as JSON plus the
  enrollment photos, with no pickle, so importing a file can't run code. Import
  adds new employees and new samples, and skips conflicts, for example an
  employee ID that already belongs to someone else on this computer. The file
  contains face photos: keep it private. Releases never include employee data.

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

## Per-face quality and guidance

Employee profiles provide a **Delete** button beneath each evidence thumbnail.
Confirmation removes that crop, its full-frame companion and metadata; enrollment
samples and attendance records are retained. Reports will show the deleted image
as unavailable. Enrollment live-camera and upload previews have a 380-pixel minimum
height, with scrolling to keep the dialog actions accessible on smaller screens.

Enrollment supports multiple samples per employee: use **Add samples** for frontal,
slight left/right, and slight up/down views with both eyes visible. All accepted
encodings are appended and considered during identity matching. Auto capture stops
at eight collected samples; manual capture/upload can add more. There is no guided
angle-coverage check, and extreme profiles may fail detection or quality checks.
Every accepted sample now retains its original photo as well as its face encoding.
**Employee Profile → Enrolled samples** shows one numbered tile per encoding,
separately from attendance evidence. Click a tile to view the original photo.
The first available saved sample is the directory portrait. Activity details
also include all available enrolled sample photos for that employee.

Old catalogs remain compatible. Previously, only a separate directory portrait
was saved, so old sample tiles display **Photo unavailable** when no original is
linked. These encodings remain active; missing originals cannot be reconstructed
from encodings. Adding new samples saves new photos without removing old encodings.
New images live in `enrollment_photos/samples/`; their filenames are stored beside
the corresponding vectors in the same atomic catalog update. Only accepted photos
are retained. Removing a sample removes its corresponding original; deleting an
employee removes that enrollment's sample originals. Attendance history is kept.

Each analyzed face is checked independently before identity encoding and PAD:
minimum original-pixel size, clipped boundaries, exposure, blur, valid face mesh,
and frontal pose geometry. Low-quality detections remain tracked, with an amber
prompt such as **Move closer**, **Improve lighting**, **Hold still**, or
**Face the camera**. Overlapping face boxes receive **Separate overlapping faces**.
Quality loss clears that person's identity confirmation, accumulated presence,
PAD/expression state, and capture evidence. It does not reset another person's
verification. Fresh checks must pass again before attendance can be saved.

These are image-suitability heuristics, not proof of liveness or a complete
occlusion detector. Pose guidance uses normalized 2D landmark geometry, not
calibrated yaw/pitch angles. Quality thresholds need validation with the actual
camera, lighting, glasses, and users. Existing MiniFASNet PAD and configured
expression checks still apply to unmodified camera pixels; no MiDaS or matting
model is added. No additional head-turn challenge is required.

**Verified Today** now counts distinct employees with committed attendance on
the local calendar day, loaded by the persistence worker, refreshed after saves
and periodically (including report deletions). It displays `-` until storage has
provided a current-day count. **Analysis** shows recognition processing time in
milliseconds beside the queue, independently of camera FPS.

## Performance tuning

All tuning defaults are in [face_attendance/config.py](face_attendance/config.py).
The default detector scale is 0.5 (640 × 360 at the default camera resolution).
Existing saved settings retain their overrides; choose 0.50 in Settings and
use Save & restart to upgrade an existing 0.25 configuration. Identity tolerance
is unchanged; encoding is scheduled separately from detection.

| Setting | Default | Meaning |
| --- | --- | --- |
| `camera_width / camera_height` | 1280 / 720 | Requested camera resolution |
| `target_fps` | 30 | Camera request and maximum UI refresh rate |
| `detection_scale` | 0.5 | YuNet input size relative to camera frame |
| `detection_interval` | 2 | Minimum source-frame gap between detections |
| `recognition_interval` | 3 | Minimum frame gap for unconfirmed/final verification encodings |
| `stable_recheck_sec` | 0.5 | Periodic re-encoding of stable tracks |
| `face_tolerance / identity_margin` | 0.5 / 0.035 | Acceptance threshold / competing-employee separation |
| `quality_min_face_px` | 80 | Minimum original face width/height; cannot be below PAD minimum |
| `quality_min_sharpness` | 25 | Laplacian variance on a 96 × 96 face; camera-specific tuning |
| `min_confirmation_frames` | 2 | Matching encoded observations before identity confirmation |
| `capture_after_sec` | 3 | Required verified presence |
| `cooldown_sec` | 30 | Persistent employee cooldown |
| `detection_fresh_sec / identity_fresh_sec` | 1.2 / 2.5 | Maximum trusted observation ages |
| `max_result_age_sec` | 1.5 | Drop late worker results |
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
For a slower CPU, `--scale 0.25` reduces detector work. Larger detector inputs
can help locate distant faces, but cannot recover missing detail for recognition. Validate tolerance/ambiguity margin against your own employees and camera
lighting. Do not assume a higher camera FPS improves recognition.

An offline benchmark is available with
`python scripts/benchmark_detection.py --image face_attendance/assets/samples/live.jpg`.
It repeats a portrait to compare 1/3/5-face processing load at scales 0.25 and 0.5;
it is not a live camera accuracy or spoof-resistance evaluation. In one local
run, median analysis times at scale 0.5 were 38/67/107 ms for 1/3/5 faces (12
samples after warmup, CPU with one OpenCV thread). Validate sustained latency,
missed detections, false matches/rejections, and time to check-in on real groups,
including crossing paths and photo/screen replay attempts.

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
screen examples, with provenance in `face_attendance/assets/samples/NOTICE.md`.
Passing these three examples does not validate arbitrary prints, video replays,
paper masks or 3D masks. The implementation follows [UniFace's MiniFASNet
documentation](https://yakhyo.github.io/uniface/modules/spoofing/) and centered,
clipped crops; it retains the existing local OpenCV runtime and bundled weights.

## Live license plate recognition (optional)

Install optional CPU OCR in its own environment to avoid conflicting OpenCV wheels:

```sh
python3.12 -m venv .venv-plates
.venv-plates/bin/python -m pip install -r requirements-plates.txt
venv/bin/python dashboard.py
```

The source checkout detects `.venv-plates` automatically. For another location, set
`SV_PLATE_PYTHON` to its absolute Python executable path. Select **License Plates**.
Plate detection uses FastALPR
with Fast Plate OCR; models download on first start (internet required), then use the local cache.
The feature is implemented separately in `license_plate/` and does not write attendance records.

Choose a USB camera index or an RTSP/HTTP stream, confidence threshold, OCR interval,
consecutive confirmation count, repeat cooldown and output folder. Press **Start camera**.
If attendance is already running on the same configured source, its clean frames are shared.
Otherwise the plate screen opens its own camera with the existing reconnect and timeout handling.
Stop plate recognition before switching attendance to that camera. A camera state change stops
plate recognition; start it again to use the new state.

**Capture plate** saves plates meeting the confidence threshold on the next reading.
Enable **Automatic capture** to save a plate after consecutive matching readings, with
a cooldown per recognized plate. Manual captures bypass stability and cooldown checks.
Each saved event includes the full frame, a plate crop and a row in `captures.jsonl`;
the default folder is `plate_captures/`. The screen shows the latest 200 captures in the
selected capture folder, restored when the screen opens or recognition starts; the JSONL file retains the complete saved history. Times are recorded in UTC.
Select a recent row to inspect its saved crop; session track IDs are visible in the list.
History loads metadata first and decodes the selected crop on demand.
Use the gear button in the camera header to expand camera and capture settings.
Camera controls require **Control engine** access; saving requires **Export data** access.

The preview uses the same floating camera controls as Live Monitor: **Start/Stop**,
**Capture**, **Auto on/off**, **Restart**, settings and the captures folder.
Use the expand icon or **More → Enter full screen** for a camera-focused view;
press **Escape** or the exit icon to restore the dashboard and its previous panels.
Video reads the latest camera frame independently of OCR, skips unchanged frames
and avoids conversion while this page is hidden. The preview targets 25 updates/sec;
actual frame rate depends on the camera and hardware. OCR timing is shown beside
the resolution. Stale or reconnected-camera results are discarded before capture.

Recognition depends on plate visibility, lighting and the model's regional coverage.
Validate results against your local plates before relying on automatic captures.
See the [Fast Plate OCR documentation](https://ankandrew.github.io/fast-plate-ocr/latest/).

### Truck maintenance demo

In **License Plates → Truck Maintenance**, choose **Load demo list** on an empty list.
The three sample trucks cover overdue, due within seven days, and clear service dates,
relative to the day the demo is loaded. Replace a sample plate with the plate on your
test truck, edit its service task and due date (`YYYY-MM-DD`), and click
**Save maintenance list**. The local registry is `plate_maintenance.json`.
You can add or remove trucks; duplicate normalized plates and invalid dates are rejected.
Editing and saving require Export data access. Loading demo data never replaces an existing list.

Start recognition and show the test plate. After the configured consecutive readings,
the maintenance panel displays the truck, service and due date, with an OVERDUE or
DUE SOON visual alert. Checks work with auto capture off. Multiple confirmed plates
are checked together. Unknown plates display NOT REGISTERED; they are never treated as clear.
Matching ignores punctuation and case, but does not guess OCR substitutions such as O/0.
This demo uses dates only, not mileage, engine hours or an external maintenance system.

Track #1, #2, etc. are recognition-session IDs associated with normalized plate text.
They expire after ten seconds of absence and restart with recognition. They are not
physical vehicle tracks: OCR changes can split an ID, and identical plates share one.
Old captures without IDs show a dash. For production truck movement tracking, add a
vehicle detector and spatial tracker (ByteTrack or BoT-SORT), associate plate crops
with each vehicle, then confirm OCR across that track. Keep the fleet plate as the
maintenance identity, not the temporary track number.

For a fixed truck gate, start with the existing CPU FastALPR pipeline and benchmark
exact plate accuracy on your own daytime, nighttime and moving-truck recordings.
Use a camera view dedicated to the plate, short exposure and adequate lighting.
Validate regional formats (including Cambodian plates if applicable), particularly
multiline plates and Khmer text; fine-tune detection/OCR if your recordings show gaps.
Set the confidence and confirmation thresholds from those results; the defaults
(0.8 confidence, three readings, 0.5-second interval) are demo starting points.
Do not automatically release or block trucks from an uncertain OCR reading; provide
operator confirmation and retain the captured frame as evidence.

References: [FastALPR](https://github.com/ankandrew/fast-alpr) and
[Ultralytics tracking](https://docs.ultralytics.com/modes/track/).

## Live Monitor automatic unknown-face captures

In **Settings → Telegram Notifications**, enable **Automatic face capture** to
save unknown / not enrolled observations after two seconds of unknown status.
Each tracked person gets a separate cropped JPEG and UTC JSON record under
`<capture_dir>/unknown/<event_id>/`. Live Activity shows the saved face. The
existing Captures folder button opens the parent folder. Enrolled employees
continue through the verified attendance capture flow and its separate records.

Unknown observations do not establish identity, employee status, or liveness;
a registered person who is not recognized can also appear as unknown. Enrollment
is required before the system can attach a name. Pause stops automatic unknown
captures. Overlapping faces and stale recognition frames are deferred. Captures
are deduplicated per track for one hour; leaving and returning with a new track
can produce another observation.

To deliver the individual crops, configure the bot token and Chat ID and enable
**On unknown face**. Attendance photos use **On capture**. Telegram sends run on
its existing background queue; simultaneous unknown people no longer suppress
each other's alerts. Local records are retained when Telegram is disabled or
unavailable. Delivery is best effort; queued notifications are not a persistent
outbox and are not automatically retried after process exit or network failure.
Saved face records remain until removed by the operator.

## Separate detection sound alerts

**Settings → Built-in sounds** provides a master mute and individual switches
for all 14 local tones. Use the sound selector and **Test alert sound** in Live
Monitor to hear each tone (preview works even when alerts are muted).

| Alert | Trigger |
| --- | --- |
| Face detected | A face stays visible for at least 0.35 seconds |
| Inactive employee / attendance blocked | An inactive matched employee remains visible for 1.5 seconds |
| Unknown / not enrolled | Unknown status persists for 1.5 seconds with acceptable quality |
| Enrolled employee recognized | Identity matched; does not imply verified attendance |
| Multiple faces in view | More than one visible face for 1.5 seconds |
| Adjust position or lighting | Poor quality or overlapping faces for 1.5 seconds |
| Presentation check rejected | A recognized face has a rejected model score for 1.5 seconds |
| Liveness action needed | A recognized face passes presentation checks but awaits liveness for 3 seconds |
| Verification complete | Identity, quality, presentation and liveness checks pass |
| Attendance saved | Attendance persistence succeeds |
| Error | Engine or attendance-save failure |
| No face in view | All faces leave for at least 2 seconds |
| Camera disconnected | A running camera loses its connection |
| Camera reconnected | Connection returns after a disconnect; initial startup is silent |

Recommended defaults enable all tones. For a busy entrance, mute **Face detected**,
**No face in view**, and **Multiple faces in view** while keeping unknown,
presentation rejection, attendance results, and camera health alerts enabled.
Detection alerts are debounced, coalesced across simultaneous people, and emitted
once per tracked condition; cooldowns and priorities prevent overlapping sounds.
Pause and engine stop silence automatic detection alerts. Presentation rejection
is a model result, not proof of an attack; an unknown face may be an enrolled
person whom recognition could not match.

## Active and inactive employees

In **Enrolled Employees**, open a person's **Edit** menu and select **Mark inactive
(terminated)** or **Reactivate employee**. The directory and profile display the
employment status; use the **Active employees** and **Inactive employees** filters.
Existing records default to active. Inactivation keeps the permanent ID, face
templates, photos, and attendance history, so a returning person remains
recognizable. It applies to every enrollment alias sharing that permanent ID.
Updating photos, renaming, exporting or importing employees retains the status.
Imports preserve the destination's status for an employee already enrolled there.

Live Monitor and Liveness Monitoring display **INACTIVE** and block attendance
for an inactive matched identity, even if all liveness checks pass. The final
attendance commit also re-reads employment status, preventing a queued job from
saving after an in-process deactivation. Already committed historical attendance
is retained. Reactivation restores normal attendance eligibility after the catalog
reload; normal verification and cooldown rules still apply.

By default, a confirmed inactive identity visible for another second produces
an individual JPEG and UTC JSON record in `<capture_dir>/inactive/<event_id>/`.
The record includes employee ID/name, blocked attendance status, and current
liveness/presentation check results. These are scan observations, including
pending or rejected liveness, rather than attendance records or proof that a
physical person was present. Quality, overlap, freshness, camera-generation,
pause, and per-track deduplication checks apply. A distinct inactive-employee
sound, Live Activity entry, notification and monitoring alert identify the event.
The **Inactive** card counts currently visible matched inactive employees.

**Settings → Telegram Notifications** has separate **Inactive employee capture**
and **Inactive employee Telegram alert** switches. The bot receives the cropped
face with the employee name/ID, UTC capture time and an attendance-blocked caption
when configured. Disabling capture or Telegram never permits inactive attendance.
The inactive sound has its own switch under **Built-in sounds**. Camera testing
and real Telegram delivery should be checked with your installation; automated
tests use synthetic frames and mocked notification transport.
