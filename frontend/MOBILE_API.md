# Flutter enrollment with the existing Python backend

Implemented on `multiple-angle-with-mobile-apis`, based on remote commit `49fde82` (verified by fetching origin).

## Reused components

- `face_attendance.face_backend.OpenCVFaceBackend`: YuNet detector and 128-dimensional normalized SFace vectors. Existing Face Mesh remains available for desktop expression checks; mobile pose uses UniFace HeadPose.
- `face_attendance.enrollment.EnrollmentService`: employee mapping, one-face validation, lighting/sharpness checks, catalog paths and existing employee directory.
- `face_attendance.anti_spoof.AntiSpoofService`: existing bundled MiniFASNet ensemble.
- `face_attendance.template_store`: versioned SFace catalog and atomic publication, compatible with existing multi-template recognition.
- Existing attendance database, employee records, attendance rules and desktop enrollment are retained.

The opt-in API runs separately. It uses an additional SQLite database for unfinished sessions, encrypted sample vectors and audit events; it does not migrate the attendance tables. Employees use existing string IDs and JSON mappings, rather than invented integer employee foreign keys.

## Start

From the Python repository:

```sh
venv/bin/python -m pip install -r requirements-mobile.txt
venv/bin/python scripts/run_mobile_api.py
```

For a machine also running optional plate recognition, use a dedicated mobile
environment instead: `python3.12 -m venv .venv-mobile`, then install
`requirements-mobile.txt` with `.venv-mobile/bin/python -m pip install -r requirements-mobile.txt`
and launch with that interpreter. The plate dependencies require
`opencv-python-headless`, while UniFace requires `opencv-python`; both distributions
write the same `cv2` module. Do not combine them in one environment. The reviewed
shared environment contained headless OpenCV 5 requiring NumPy 2 alongside this
project's NumPy 1.26.4 pin; `python -m pip check` exposes this conflict.
The declared mobile requirements retain OpenCV 4.10 and NumPy 1.26.4.

Sign in on mobile using an existing desktop administrator username and password. The account must be active and have `manage_employees` permission. If you need a new admin:

```sh
venv/bin/python scripts/run_mobile_api.py --create-admin
```

The launcher prepares and verifies UniFace ResNet18 weights before serving requests. To prepare the model alone, run `venv/bin/python scripts/run_mobile_api.py --prepare-model`. Weights are cached in `.cache/uniface/` (excluded from Git), or `UNIFACE_CACHE_DIR` if specified. The launcher prompts for username/name/password, creates the account in the configured user database, and starts the API. It generates a persistent `.mobile_api.key` on first run (owner-only permissions, excluded from Git). Back up this key securely with the session database. `SV_ENROLLMENT_KEY` can override it. Direct uvicorn startup is still supported when that variable is set.

Mobile sign-in issues an eight-hour session token. Tokens are stored as hashes on the server and kept only in mobile memory. Sign-out revokes the token. Account disablement and permission changes take effect on the next API request. Failed sign-in is limited to five attempts per username/client IP in five minutes. The optional `SV_MOBILE_TOKEN` compatibility token remains supported only if explicitly configured; the mobile UI uses account sign-in.

Use HTTPS: either pass `--ssl-certfile cert.pem --ssl-keyfile key.pem` to the launcher or terminate TLS in a reverse proxy. Release mobile builds refuse plain HTTP, and the launcher warns when serving HTTP on the network. On start it prints the URL to enter in the mobile app's Server settings. API docs are at `/docs`; `/health` is an unauthenticated connection check (the app requires `apiVersion` ≥ 2). Run one API worker and avoid concurrent catalog changes from desktop enrollment or CLI while mobile completion publishes. A running desktop dashboard (`python dashboard.py`) detects the published catalog within a few seconds and reloads its engine automatically (the camera briefly restarts).

Active templates retain the existing local catalog's plaintext format; protect its directory with OS permissions and encrypted disk storage. Pending vectors are encrypted. Server-side original mobile photos are not retained.

## Configuration

Catalog and employee paths are read from the desktop `settings.json` when present. `SV_ENCODINGS_PATH` and `SV_EMPLOYEES_PATH` override them; relative paths resolve against the backend repository root.

- `SV_ENROLLMENT_DB_PATH`: absolute path or path relative to Python repository root. Default: `mobile_enrollment.db` beside the current attendance database. Parent directories are created; SQLite WAL and foreign keys are enabled; connections close after requests.
- `SV_POSE_THRESHOLD`: minimum turn, default 15°.
- `SV_POSE_FRONT_MAX`: frontal tolerance, default 10°.
- `SV_POSE_YAW_MAX` / `SV_POSE_PITCH_MAX`: turn limits, default 35° / 30°.
- `SV_POSE_ROLL_MAX`: tilt limit, default 12°.
- `SV_POSE_YAW_SIGN` / `SV_POSE_PITCH_SIGN`: coordinate adapters, default `1`; use `-1` only after physical-device calibration demonstrates a reversed convention.
- `SV_ENROLLMENT_DISTANCE`: maximum cross-angle SFace cosine distance, default 0.5. Duplicate-employee rejection uses the desktop `face_tolerance` (default 0.5) against normalized vectors and names the existing employee. These policies require calibration against your real deployment.

For database relocation, stop the API and use SQLite's backup API to create a consistent copy (or checkpoint and close before copying); do not copy a live `.db` alone while WAL writes are active. Set the new path and retain the encryption key. Existing attendance DB relocation remains under the desktop's existing database configuration.

## Contract

Authenticated calls use `Authorization: Bearer <accessToken>` from sign-in.

- `POST /auth/sign-in`: `{"username":"admin","password":"your password"}` returns `accessToken`, `expiresAt`, and public user fields.
- `GET /auth/me`: validates the session.
- `POST /auth/sign-out`: revokes it.
- `GET /employees`: lists enrolled employees and pending mobile enrollments, including employee IDs, names and enrollment IDs. Different employees can share a display name; mobile sessions use distinct catalog labels when needed.

- `POST /enrollments`: `{"employeeId":"E001","name":"Alice","reenroll":false}`. Returns an existing active session for that ID, allowing resume. Existing enrolled employees require `reenroll:true`.
- `GET /enrollments/{id}`: persistent accepted progress.
- `POST /enrollments/{id}/captures`: `{"image":"<base64 JPEG>"}`. Each upload is validated against the current required pose. Repeat the call to retry a rejected image. Rejections return progress with a human-readable `rejectionReason`; they never count as accepted.
- `POST /enrollments/{id}/complete`: only publishes after seven accepted angles. Repeating completion is safe. Successful publication replaces that employee's vectors; interrupted/failed capture never changes the existing working profile. Completion rechecks every pending vector against the current catalog and returns HTTP 409 naming the existing owner if another employee now matches. Pending progress remains available to review or cancel; no conflicting profile is published.
- `GET /employees/{employeeId}/enrollment`: mobile session or legacy enrollment status.
- `DELETE /enrollments/{id}`: cancel incomplete enrollment and delete its pending vectors. Completed profiles remain managed by the desktop employee screen.

Example progress:

```json
{"employeeId":"E001","enrollmentId":"uuid","status":"IN_PROGRESS","totalAngles":7,"completedAngles":1,"progressPercentage":14.29,"nextAngle":"LEFT","angles":[{"angle":"FRONT","status":"ACCEPTED"},{"angle":"LEFT","status":"PENDING"}],"rejectionReason":null}
```

The actual `angles` list always contains seven entries. Completed sessions retain 100% progress after pending encrypted vectors are removed.

## Real-time camera protocol and validation

Flutter uses `startImageStream()` with Android YUV420 and iOS BGRA8888. Conversion honors each plane's row and pixel strides. JPEG encoding runs in a separate Dart isolate, resizes to at most 720 pixels per side and preserves the unmirrored image. Android applies sensor/device rotation; the current iOS AVFoundation plugin rotates video output already, so Dart does not rotate it again. The mirrored preview remains a display transformation only.

An authenticated WebSocket at `/enrollments/{id}/stream` replaces timed photos:

1. Send `{"accessToken":"<sign-in token>"}` as the first JSON message. The server responds with `{"type":"ready", ...progress}`. Tokens never appear in the URL.
2. Send `{"type":"frame","frameId":1,"capturedAtMs":150,"targetAngle":"FRONT"}` followed by binary JPEG bytes. `capturedAtMs` is a monotonic elapsed time within the connection, not wall-clock time.
3. Wait for `{"type":"progress","frameId":1,...progress,"pose":{"yaw":0,"pitch":0,"roll":0},"stabilityProgress":0.2,"captureAccepted":false}` before sending another frame.
4. The Flutter client targets up to 6.7 sampled frames/sec, drops frames while encoding/inference is in flight, and never queues old camera frames. Actual throughput depends on the device, network and CPU.
5. Pause closes the connection and clears incomplete stability evidence. Continue reconnects; accepted angles remain saved. Only one stream may enroll an employee at a time.

The server rechecks authentication on every frame, enforces strictly increasing sequence IDs/timestamps, rejects immediate identical-frame repeats, bounds relative timestamp drift and resets evidence on disconnect. The launcher limits WebSocket frames to 2 MB with a queue of one. Configure equivalent limits if launching uvicorn directly.

UniFace's ResNet18 head-pose model runs on a BGR face crop before recognition alignment. Normalized angles use negative yaw for LEFT and negative pitch for UP; diagonals combine a horizontal turn with looking up. Confirm subject-facing instructions on physical devices before deployment.

The review corrected reversed UP/DOWN and diagonal pitch classification. Cancel
and restart unfinished sessions captured before this correction; previously
accepted samples do not carry a pose-policy version and cannot be relabeled safely.

Acceptance requires exactly one visible, high-quality face, MiniFASNet ensemble acceptance, consistent identity against the candidate and the persistent FRONT reference, and at least six valid frames spanning one second. Gaps over 750 ms or pose variation over 6° reset the stable window. Entry uses strict pose bounds; an existing window tolerates a 3° exit margin. Exit-margin frames maintain continuity but cannot supply the saved template, including after a gap or motion reset. An exponential moving average drives pose feedback. The sharpest frame within strict pose bounds supplies the SFace vector, rather than blindly taking the last frame.

The REST `/captures` endpoint remains available for compatibility and uses the same validator; it is blocked while an enrollment stream is active. The mobile app uses streaming exclusively. Physical camera images are not saved to disk by this path.

Physical-device orientation, pose-sign and presentation-attack calibration remains required. MiniFASNet RGB scoring and the timestamp/duplicate checks do not establish replay-proof liveness. Randomized challenges, calibrated model thresholds and deployment retention policies remain further hardening work. Existing prototype sessions retain accepted samples; cancel and restart any unfinished session whose captures were collected using the old solvePnP convention. Existing active SFace profiles remain unchanged until authorized completion.

Mobile publication uses existing atomic catalog writes, but employee mapping and catalog publication are two filesystem operations rather than one database transaction. Back up both files before rollout; avoid concurrent writers. For rollback, stop the API and restore the prior `encodings_sface.pickle` and `employees.json` together. The desktop never depends on the optional API or the new session database.

## Validation

`venv/bin/python -m pytest tests/test_mobile_api.py`

Flutter setup and validation are described in the mobile repository README.
