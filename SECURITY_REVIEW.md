# Security and continuous-operation review

Reviewed the desktop engine, camera/reconnect lifecycle, persistence, credentials,
template loading, optional plate OCR, and optional mobile enrollment API. This is
a source review with regression/runtime evidence, not penetration-test certification.
The deployment target supplied by the operator is macOS with a USB camera.

## Fixed findings

| Finding | Result |
| --- | --- |
| Full-frame Qt signals accumulated while the GUI was busy | One coalesced GUI notification holds the latest frame/tracks/stats. A 10,000-update blocked-GUI regression delivers one final frame. |
| Empty camera streams reset retry pressure; driver release could stop recovery | Backoff resets only after a frame, caps at 30 seconds, and cleanup failures no longer escape the reconnect loop. |
| Storage errors or optional audio failures could stop persistence | Connection/read/write failures publish unavailable state and reconnect; normal operation publishes job outcomes. Audio failure does not disable recording. Shutdown can drop outcome notifications if the bounded result queue is full; this does not itself undo committed rows. |
| Repeated restart could accumulate stuck native workers | Start refuses to replace a camera/recognition/persistence worker that has not exited. A stopped-but-stuck engine cannot clear its stop event and resume accidentally. |
| Idle macOS system sleep interrupted unattended monitoring | Engine/plate workers scope `caffeinate -i -w PID` assertions to active monitoring; stop/process exit releases them. No global power settings changed. |
| First-use packaged camera authorization was attempted on a worker thread | Packaged local-camera start/restart/autostart requests Qt permission asynchronously on the GUI thread, rechecks access before starting, and reports denial. |
| Weak/legacy password hashes and nonconstant comparison | New passwords/PINs use PBKDF2-SHA256 with 600,000 iterations and constant-time verification. Legacy accounts upgrade on successful sign-in; legacy PINs remain readable. |
| Truncated or malformed remembered session and permissive startup after DB outage | Session writes are atomic/private, malformed/expired files fail closed, and failed account-table queries require sign-in even on a new client. |
| Arbitrary pickle global loading | Template loading restricts NumPy numeric primitives, rejects object arrays, caps input size, and preserves supported numeric legacy formats. Arbitrary callable reductions are rejected. |
| Unbounded observation logs / plate session identity set | CSV observations rotate at 10 MiB with three backups; the plate unique cache caps at 10,000 and explicitly displays a lower bound after overflow. Attendance/outbox/evidence are preserved. |
| Plate capture metadata could retain camera credentials | Shared URL formatting redacts passwords, query strings and fragments. New plate files are private; low disk fails before image creation; saved metadata is flushed. |
| Telegram failures could expose bot URLs; queued photos could change destination | Error messages omit request URLs; queue generations prevent face photos from being rerouted by destination reconfiguration. No real notifications were sent during tests. |
| Mobile login/resource/stream limits were incomplete | Bounded per-IP/user throttling, HTTP body limit, connection/pending-session caps, frame/JPEG header validation, receive deadlines, authorization on reset, threadpool locking, and cancellation-safe ownership cleanup. |
| Optional plate dependencies replaced desktop OpenCV with an incompatible headless wheel | OCR can run in a bounded persistent subprocess with its own environment and deadlines. The repaired desktop has only regular OpenCV 4.10/NumPy 1.26; OCR has only headless OpenCV 4.10/NumPy 1.26. |
| Installed pip had published archive/path traversal advisories | Updated the desktop and isolated OCR installers to pip 26.2.1. |

The password work factor follows the [OWASP Password Storage Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Environment separation follows the [OpenCV Python package guidance](https://pypi.org/project/opencv-python/)
to select one OpenCV wheel per environment.

## Validation and limits

Both environments pass `pip check`. `pip-audit` reported **zero known advisories**
for the repaired desktop's 64 installed distributions and the isolated plate
environment's 16 distributions on 2026-10-19. This checks published Python-package
advisories; it does not prove that native libraries, models, hardware, configuration,
or unpublished vulnerabilities are safe. The audit tool lived in `/private/tmp`,
separate from the deployed environments.

The isolated real FastALPR backend started, processed a blank frame and closed.
Final synthetic worker/reconnect soak: 65 seconds, 1,740 camera frames observed, 1,770
preview updates, no engine errors, and all workers stopped. A physical camera-index-0
smoke also captured frames; later inventory identified the available device as
**MacBook Pro Camera**, so this is not USB-camera acceptance. The attempted longer
run was interrupted after this mismatch was discovered and does not count as a
24-hour pass. No employee catalog was loaded or face images retained by those tests.

Full regression run after dependency repair: **537 tests and 116 subtests passed**.
Subsequent focused validation of the final fixes: **49 tests and 5 subtests passed**;
the final isolated-process cleanup suite passed **20 tests**. Native macOS Qt
validation passed **24 tests and 7 subtests**. These sets overlap and should not
be added together. Compilation and whitespace checks pass.

The USB-camera soak result remains pending the correct connected device. The
previous broad run found a stale UI test
that assumed Settings was the final navigation page; the test now finds the
Settings screen by type, preserving its actual sound-setting assertions.

## Deployment requirements that remain

- Complete the physical USB-camera soak and the hardware acceptance matrix in
  [OPERATIONS.md](OPERATIONS.md), including representative enrollment and both
  monitors if used. No software can guarantee operation during forced sleep,
  closed-lid sleep, power loss, or a native driver that never returns.
- Configure individual operator accounts or a PIN; an intentionally unconfigured
  installation still has no dashboard login boundary. Protect the macOS user
  account, FileVault, settings, remembered session, employee data and backup media.
- Approve retention/capacity/backups for attendance, outbox, audit rows, plate
  history and evidence. Existing production data was not purged. RAM-queued
  attendance is not a durable offline journal and can be lost on process/power loss
  before database commit; use a UPS and reconcile failed/uncommitted events.
- Use TLS for remote databases/mobile enrollment. Mobile caps are process-local:
  deploy one API worker or add a shared limiter/ownership store. Configure transport
  message limits too; the application check happens after ASGI receives a message.
- Treat catalogs as approved local data even with restricted loading: crafted
  NumPy allocations can still exhaust resources. Models and RGB anti-spoofing are
  probabilistic and require camera-specific real-world validation.
- The OCR child has a hard timeout; native face inference/USB calls remain inside
  the desktop process and cannot safely be killed as individual threads. A hung
  native component requires operator/application recovery, not repeated starts
  that create overlapping workers. No LaunchAgent was installed or enabled.
