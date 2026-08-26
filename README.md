# Employee Face ID Recognition

Real-time face recognition system: recognizes enrolled employees and
sounds an alert when an unrecognized face is detected.

## How it works

1. **Enroll employees** — capture each person's face once (or a few
   times from different angles) and store a numeric "encoding" of
   their face.
2. **Run recognition** — the webcam (or an IP camera stream) is
   scanned in real time. Each detected face is compared against the
   enrolled encodings:
   - **Match found** → green box + employee name, logged as `KNOWN`.
   - **No match** → red box + `UNKNOWN`, alert sound plays, logged as
     `UNKNOWN`.

All events are written to `attendance_log.csv` with timestamps.

## Setup

```bash
pip install -r requirements.txt
```

> Note: `face_recognition` depends on `dlib`, which needs CMake and a
> C++ compiler to build. On Windows, installing via `conda` is often
> easier: `conda install -c conda-forge dlib`. On Linux/Mac,
> `apt install cmake` / `brew install cmake` first, then pip install
> as normal.

Generate the alert sound once (creates `alert.wav`, no external audio
file needed):

```bash
python generate_alert_sound.py
```

## Enroll employees

```bash
python enroll_faces.py --name "Jane Smith"
```

This opens your webcam — press **SPACE** to capture, **Q** to cancel.
Or use an existing photo:

```bash
python enroll_faces.py --name "Jane Smith" --image photos/jane.jpg
```

Run it a few times per person with different angles/lighting to
improve accuracy — new samples are appended, not overwritten.

## Run real-time recognition

```bash
python recognize.py
```

Options:

```bash
# Use an IP camera / RTSP stream instead of the laptop webcam
python recognize.py --source "rtsp://user:pass@192.168.1.50:554/stream1"

# Stricter matching (fewer false positives, more false "unknowns")
python recognize.py --tolerance 0.45

# Process every frame instead of every 2nd (more accurate, slower)
python recognize.py --process-every 1
```

Press **Q** in the video window to quit.

## Tuning notes

- **`--tolerance`**: default `0.5`. Lower = stricter (fewer false
  accepts, but a known employee might occasionally get flagged as
  unknown in bad lighting). Higher = looser. Common range: `0.4–0.6`.
- **`--process-every`**: detection is the expensive step. On a modest
  laptop CPU, processing every 2nd–3rd frame keeps things responsive
  without hurting real-time feel.
- **Alert cooldown**: set in `recognize.py` as `ALERT_COOLDOWN_SEC`
  (default 3s) so the alert doesn't spam-play continuously while an
  unknown person stands in frame.
- **Lighting/angle**: enroll each employee 2-3 times (front, slight
  left/right) for more robust matching.

## Scaling this up later

- **Multiple cameras**: run one `recognize.py` process per camera
  (each with its own `--source`), or refactor into a multi-threaded
  version that pulls from several RTSP streams into one recognition
  loop.
- **Better accuracy/speed**: swap `face_recognition` (dlib-based) for
  **InsightFace** (ONNX Runtime) — noticeably faster and more accurate
  on angled/low-light faces, especially with GPU acceleration.
- **Edge deployment**: this same logic runs on a Raspberry Pi (use a
  lighter detector like `mtcnn` or `mediapipe`, and consider a Coral
  USB TPU for detection speed).
- **Central logging/dashboard**: swap the CSV logger for a small
  SQLite/Postgres table and add a simple web dashboard to review
  attendance and unknown-face snapshots.
- **Save unknown-face snapshots**: add a `cv2.imwrite()` call in the
  `UNKNOWN` branch of `recognize.py` so security can review who
  triggered each alert.

## Files

| File | Purpose |
|---|---|
| `enroll_faces.py` | Register a new employee's face |
| `recognize.py` | Real-time recognition + alert loop |
| `generate_alert_sound.py` | Creates the alert beep WAV |
| `encodings.pickle` | Generated — stores enrolled face encodings |
| `attendance_log.csv` | Generated — event log |
