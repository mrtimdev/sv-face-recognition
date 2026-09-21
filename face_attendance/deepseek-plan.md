I have the full picture now. Here's what I verified before planning:

**Recon results**
| Item | Finding |
|---|---|
| UI today | `cv2.imshow` single window in `face_attendance/runtime.py`; overlays/HUD in `face_attendance/ui.py` (`UIRenderer`, `Layout`) |
| Threads | camera + recognition + persistence workers, GUI loop owns tracks/state |
| Enrollment | `enroll_faces.py` (module-level `ENCODINGS_PATH`/`EMPLOYEES_PATH`, atomic `save_encodings`/`save_employee`, single-face rule) |
| Data | SQLite `attendance`, `attendance_cooldowns`, `attendance_outbox`; `{name: [128-d]}` pickle + `employees.json` |
| Deps installed | **PyQt6 6.10.2 / Qt 6.10.0 works**, Pillow 11.3, requests, python-dotenv, simpleaudio — **no new deps needed for a Qt dashboard** |
| Qt smoke test | `QImage(np.data, BGR888)` + offscreen `QApplication` both work → headless testable |
| Baseline | `./venv/bin/python -B -m unittest discover -s tests` → **40 tests OK** |
| Constraints | `Config` is a **frozen** dataclass (settings changes ⇒ engine restart); recognition/DB/snapshot must stay off the UI thread; queues bounded; single-face enrollment; no anti-spoofing; no shift policy exists |

---

# Plan — Admin Dashboard UI for `sv-face-recognition`

## 1. Goal & guiding constraints

Build a **PyQt6 desktop admin dashboard** (ships with the app, works offline, reuses existing workers) with 5 screens:

1. **Live Monitor** — camera preview + engine control
2. **Employees** — enroll by live capture *and* by photo upload, with name/ID input
3. **Settings** — camera (webcam index / IP-RTSP URL / file), resolution, FPS, recognition + storage config
4. **Real-time Attendance** — streaming check-in feed + unknown-face alerts
5. **Attendance Report** — filter/aggregate/export from SQLite

Non-negotiables preserved from `testing-skill.md` and README:
- Recognition correctness > FPS; recognition/DB/snapshot I/O stays off the UI thread; bounded queues.
- Snapshot is the clean frame (no overlays); success shown only **after** SQLite commit.
- `python main.py --source 0` and `python enroll_faces.py ...` keep working unchanged.
- Existing `{name: [128-d]}` pickle + `employees.json` formats unchanged.

**Stack decision (recommended):** native PyQt6 dashboard. It is already installed and verified, requires zero new dependencies, embeds the existing numpy frames fast (`QImage(..., Format_BGR888)`), and can reuse `UIRenderer`, `CameraManager`, `RecognitionService`, `FaceTracker`, `AttendanceService`, `PersistenceWorker` as-is. (Alternative: web dashboard — rejected by default because no Flask/FastAPI is installed, RTSP credential handling over HTTP adds risk, and video streaming would be much heavier. Can be revisited later.)

---

## 2. Target architecture

```text
Qt MainWindow (UI thread only, no I/O)
  ├─ Live Monitor / Realtime / Report / Employees / Settings  (QStackedWidget)
  └─ AttendanceEngine (QObject, signals) ─────────────┐
        runs the coordination loop on its own QThread │
        owns: CameraManager, RecognitionWorker, FaceTracker,
              AttendanceService, PersistenceWorker  (UNCHANGED components)
```

Key rules:
- **One engine instance per process**, guarded by a lock file → never two engines on one SQLite file.
- The UI never touches `AttendanceRepository.connection` (persistence thread owns it). Reports use a **separate read-only connection** (`AttendanceReader`), which WAL supports concurrently.
- Qt signals carry immutable snapshots (`FramePacket`, `FaceTrack` state dicts, `SaveResult`) — no cross-thread mutation.
- Enrollment reuses the **engine's camera** (subscribe to the `LatestValue` frame mailbox) instead of opening the device a second time; falls back to a temporary capture session when the engine is stopped.

```text
Camera thread → LatestValue frame ─┬→ AttendanceEngine loop → overlay render → QImage → VideoView
                                   └→ Enrollment capture session (shared, no 2nd device open)
```

---

## 3. New module layout

```text
dashboard.py                          # top-level launcher: python dashboard.py  (matches main.py/recognize.py style)
face_attendance/
  settings.py                         # Settings dataclass + settings.json load/save/validate → Config
  enrollment.py                       # shared enrollment service (refactor of enroll_faces.py core)
  report.py                           # read-only attendance queries + CSV export + summary stats
  dashboard/
    __init__.py
    __main__.py                       # python -m face_attendance.dashboard
    app.py                            # QApplication, MainWindow, sidebar nav, routing, toasts
    theme.py                          # QSS palette derived from ui.py (DARK 27,24,20 / GREEN 100,225,95 / RED / AMBER / LINE 61,60,53)
    bridge.py                         # numpy BGR → QImage/QPixmap (keeps frame alive, no shadowing bugs)
    engine.py                         # AttendanceEngine(QObject): start/pause/resume/reload_catalog/shutdown + signals
    widgets/
      video_view.py                   # aspect-preserving video canvas + overlay + status pill
      stat_card.py, status_pill.py, toast.py, empty_state.py
    screens/
      live.py                         # Live Monitor
      realtime.py                     # Real-time Attendance
      report.py                       # Attendance Report
      employees.py                    # Enrollment (live capture + upload)
      settings.py                     # Camera / Recognition / Storage / Appearance
```

File names avoid collisions with existing modules by living under `dashboard/screens/`.

---

## 4. Phase 0 — groundwork refactor (no UI yet, tests stay green)

| Task | Detail |
|---|---|
| `face_attendance/enrollment.py` | Move `load_encodings`, `save_encodings`, `save_employee` and the validate-and-append logic out of `enroll_faces.py` into a service: `enroll_image(image_bgr, name, employee_id=None, paths=...) → EnrollmentOutcome`. Return structured results (`ok, samples_added, total_samples, total_people` / `no_face` / `multiple_faces` / `error`) instead of printing. **Reject multi-face and zero-face exactly as today.** |
| `enroll_faces.py` | Becomes a thin CLI wrapper over the service (same flags, same printed output) → backward compatible. |
| `enrollment.validate_frame(image)` | Add quality gates used by the live-capture flow: exactly one face, min face height (e.g. ≥120 px), blur (variance of Laplacian), mean brightness range. Returns reasons so the UI can coach the user. |
| `repository.py` additions | Read-only query methods on a **separate connection** in `report.py`: `records_between(start_epoch, end_epoch, employee_id=None, limit, offset)`, `daily_summary(...)`, `employee_totals(...)`, `outbox_counts()`. Reuse `parse_legacy_time` semantics (legacy naive timestamps = machine local time) and store/compare `recorded_at_epoch` (UTC epoch). |
| `ui.py` addition | `UIRenderer.render_overlay(frame, tracks, now, wall_time, ...)` → brackets/animations only, **no HUD, no resize**, so the Qt panel can show native-resolution video with Qt-drawn stat cards. Existing `render()` untouched → `tests/test_ui.py` assertions still valid. |
| `settings.py` | `Settings` dataclass persisted to `settings.json` (project-local, **gitignored**), `to_config()` building `Config`, validation by reusing `Config.__post_init__` (catch `ValueError` → UI error), `mask_source()` for RTSP credentials. |

**Exit criteria:** 40 existing tests still pass + new tests for enrollment service, report queries and settings store pass.

---

## 5. Screen specs

### 5.1 Live Monitor (`screens/live.py`)
- Large `VideoView` (engine frames + overlay), aspect-correct, placeholder when disconnected.
- Controls: **Start / Pause attendance / Restart engine**, "Simulate/scan" toggle, screenshot button.
- Stat cards: camera status (`CONNECTING/CONNECTED/RECONNECTING/DISCONNECTED` from `CameraManager.status`), camera FPS, preview FPS, active faces, enrolled employees, DB path, recognition latency (`RecognitionResult.elapsed`), storage queue health (`jobs.qsize()/maxsize`).
- Problem banner from `attendance.storage_error` and `result.error`; toast on each `SaveResult`.
- Exit: graceful `shutdown()` joining camera → recognition → persistence (identical order to `AttendanceApplication.run()`'s `finally`).

### 5.2 Employees / Enrollment (`screens/employees.py`)
- Left: employee table — display name, employee ID, sample count, canonical name, actions (**Add samples**, **Delete samples**, **Remove employee**).
- Right: enrollment wizard with two tabs:
  - **Live capture** — Start camera → coach text from `validate_frame` ("Move closer", "One face only", "Hold still") → capture **3 samples** with a 3-2-1 countdown and progress ring → per-sample previews with a delete option.
  - **Upload photo** — file dialog + drag & drop (`.jpg/.jpeg/.png/.bmp/.tiff`), preview with detected face rectangle, single-face validation message, optional auto-crop to face box.
- Inputs: **Display name** (required), **Employee ID** (optional; auto-suggest `EMP-###`; immutable once assigned — attempts to change show the existing migration warning) and a warning when an identical encoding already belongs to a different employee.
- Save → `enrollment.enroll_image` (atomic pickle + `employees.json` writes) → `engine.reload_catalog()` → refresh table + stat cards.
- Rules surfaced in UI: images with multiple faces are rejected; aliases may share an employee ID; removing enrollment never deletes historical attendance rows (names preserved).

### 5.3 Real-time Attendance (`screens/realtime.py`)
- Medium video + live table of **today's** records, newest first: time, name, employee ID, verified presence seconds, status, snapshot thumbnail (click → full image).
- Live per-face state strip: track ID → name → `State` (DETECTING…SUCCESS/ERROR) → verification progress (%) → cooldown countdown.
- Filters: today / last hour; UNKNOWN-face alert list with a "no anti-spoofing at this time" disclosure.
- Counters: checked in today, unique employees, unknown-face events, cooldowns active. Updates from engine signals + a 1-second QTimer DB poll (`recorded_at_epoch >= today_start`).

### 5.4 Attendance Report (`screens/report.py`)
- Filter bar: date range (Today / This week / This month / Custom `QDateEdit`), employee search (name or ID), status, page size.
- `QTableView` + custom `QAbstractTableModel` over `report.records_between` (server-side paging via LIMIT/OFFSET) — no `pandas`.
- Summary cards: total records, distinct employees, first check-in, last check-out, days present, outbox **pending / synced** counts.
- Row actions: open snapshot, copy event ID, view raw outbox payload.
- Export: **CSV** (stdlib `csv`, UTF-8 BOM option for Excel) for the current filter; "Export snapshots list" path column.
- Explicit disclaimer in the UI: no shift/late/absent policy exists in the data model — **opt-in** "work start time" setting computes Late/On-time labels and is off by default, computed from the report query only (never written back to the DB).

### 5.5 Settings (`screens/settings.py`)
- **Camera**: source type radios — Webcam index (device probe/dropdown with a "Test" preview), IP/RTSP URL (masked password field, never logged), video file; resolution preset + custom W×H; target FPS; detection scale; detection/recognition interval; FFmpeg open/read timeout; reconnect backoff. **Test Connection** shows the negotiated resolution/FPS and a short preview.
- **Recognition**: `face_tolerance` and `identity_margin` sliders with a warning that loosening them does not fix duplicate enrollment data; `capture_after_sec`, `cooldown_sec`, `min_confirmation_frames`, `stable_recheck_sec`, `opencv_threads` (with a note about CPU saturation).
- **Storage**: db path, captures dir, CSV log path, `alert.wav` path + audio test, persistence queue size; **open folder** buttons; shows DB size and pending outbox count.
- **Appearance**: dark/light theme, panel density.
- Buttons: **Apply** (validate → save `settings.json`), **Apply & Restart engine** (required because `Config` is frozen and workers capture it at construction), **Reset to defaults**.
- Never display RTSP passwords unmasked on screen-share; store masked-by-default with a "reveal" toggle.

---

## 6. Threading & lifecycle rules the dashboard must honor

| Concern | Rule |
|---|---|
| Engine loop | Coordination loop runs on a dedicated `QThread`; UI updates only via signals. |
| Frame handoff | `numpy` → `QImage` conversion copies into a `QPixmap` immediately; the array reference is kept alive during conversion (`bridge.py`). |
| Blocking | No `cv2.waitKey`, `imshow`, or DB/snapshot work on the UI thread. |
| Enrollment vs attendance | Enrollment uses the engine's frame mailbox; while samples are being written attendance is auto-paused (a catalog write mid-verification could revoke/confirm wrongly), then resumed. |
| Catalog change | `reload_catalog()` = stop recognition worker → rebuild `RecognitionService` with the new `FaceCatalog` → rebuild `AttendanceService`/worker → start. Persistence worker keeps `employee_map`; restart it too so mapping changes apply (mapping changes currently require a terminal restart per README). |
| Settings change | Saved to `settings.json`; only applied on engine restart; UI asks for confirmation when attendance is running. |
| Shutdown | Window close → engine `shutdown()` (camera → recognition → persistence, drain accepted jobs) → release lock file. |
| Concurrency | Existing `BEGIN IMMEDIATE` + WAL + cooldown-authority behavior unchanged; report connection is read-only and never writes. |
| Secrets | RTSP URL stored in gitignored `settings.json`, never logged, masked in UI and in any support/export output. |

---

## 7. Test plan (extends existing `unittest` conventions)

All run through `./venv/bin/python -B -m unittest discover -s tests -v` (Qt cases guarded by `skipUnless` + `QT_QPA_PLATFORM=offscreen`).

| New file | Coverage |
|---|---|
| `tests/test_settings.py` | Defaults, round-trip save/load, validation errors surfaced from `Config`, source parsing (`0` vs `rtsp://…` vs file), credential masking, unknown keys ignored. |
| `tests/test_enrollment.py` (extend) | Service-level: zero face, multiple faces rejected, sample append + atomic replace, corrupt pickle raises, `employee_id` immutability, alias shares ID, `validate_frame` blur/size/brightness gates. |
| `tests/test_report.py` | Range boundaries (inclusive/exclusive), legacy naive-local vs UTC epoch, employee filter, paging, empty result, CSV content incl. UTF-8 names, outbox counts, no writes to DB (assert row counts + WAL unaffected). |
| `tests/test_engine.py` | Headless engine with fake camera + fake backend (reuse `tests/test_integration.py` `Backend`/`Capture` patterns): frames emitted, exactly one commit per event, pause/resume, `reload_catalog` picks up a new employee, graceful shutdown joins all threads, single-instance lock rejects a second engine. |
| `tests/test_dashboard_ui.py` | Offscreen Qt: window builds, screens switch, video view converts a synthetic BGR frame to a `QPixmap` of expected size at 640×480/1280×720/1920×1080, model populates from a temp DB, theme applies without missing-resource warnings. |

Manual acceptance: real webcam + RTSP soak test (README still flags this as outstanding), enroll by live capture and by upload, verify a check-in appears in Real-time and Report with the correct snapshot, and confirm no duplicate row inside `cooldown_sec`.

---

## 8. Milestones & deliverables

| Phase | Deliverable | Acceptance |
|---|---|---|
| **0** | `enrollment.py`, `report.py` queries, `settings.py`, `ui.render_overlay` | 40 old + new unit tests pass; CLI unchanged |
| **1** | Dashboard shell (`app.py`, `theme.py`, `bridge.py`, `widgets/`, `engine.py`) + Live Monitor | Launch `python dashboard.py`, live video + status + start/stop, clean shutdown |
| **2** | Employees screen (live capture + upload) | Enroll both ways; new employee recognized without terminal restart |
| **3** | Real-time Attendance screen | Check-in streams live; snapshot opens; unknown-face alerts listed |
| **4** | Attendance Report screen | Filters/paging/summary/CSV export correct against a seeded temp DB |
| **5** | Settings screen + `settings.json` persistence | Camera/recognition/storage changes apply after engine restart; invalid values blocked with messages |
| **6** | Polish + docs | README "Dashboard" section, updated `face_attendance/plan.md`, `requirements.txt` gains `PyQt6==6.10.2`, all tests green |

---

## 9. Risks & explicit non-goals

- **`Config` is frozen** → live re-tuning needs an engine restart; the UI states this clearly instead of pretending hot-reload.
- **Camera ownership** → exactly one open per device; enrollment borrows frames from the running engine.
- **RTSP credentials** in a local JSON file are a real secret-management risk; masked + gitignored + never logged, with a note to prefer a URL without embedded credentials where the camera supports it.
- **No liveness/anti-spoofing** (unchanged) → the UI must not imply it.
- **No shift/OT/late policy** exists in the schema → report shows factual aggregates only; Late/Absent is an opt-in computed label, never persisted.
- **macOS camera permission** prompt appears on first capture; document it.
- Non-goals: multi-camera concurrency, remote/multi-user auth, replacing `recognize.py`, changing the pickle/JSON/SQLite formats, adding pandas/openpyxl/Flask.

---

## 10. Open decisions before Phase 1

1. Native PyQt6 dashboard (recommended, no new deps) vs. web dashboard (needs Flask/FastAPI added)?
2. Should the dashboard replace `cv2.imshow` entirely (`main.py --ui qt`) or live alongside it as `python dashboard.py` (recommended: alongside, preserves compatibility)?
3. Do you want an application login/PIN for the admin screens (single local operator, no user store exists today)?

`face_attendance/plan.md` currently holds the raw request text — say the word and I'll replace it with this plan and start **Phase 0**.