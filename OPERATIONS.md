# macOS USB camera: continuous operation

This guide applies to the source checkout and its `venv`, with a USB camera on a dedicated Mac. Software regression tests have passed; **a continuous 24-hour hardware soak has not been performed**. Camera-driver behavior after sleep, USB resets, and system updates must be accepted on the deployment machine before unattended use.

## Launch and configure

Use the project environment, not Anaconda or the system Python. From the project directory:

```sh
cd '/Users/timdev/Desktop/works/Python Projects/sv-face-recognition'
venv/bin/python dashboard.py --help
venv/bin/python main.py --help
venv/bin/python dashboard.py --settings '/Users/timdev/Desktop/works/Python Projects/sv-face-recognition/settings.json' --start
```

These options were checked against the current command help. `--start` starts attendance after the dashboard opens and its login flow completes. It does not bypass authentication or automatically start license plate recognition. In dashboard Settings, select the USB camera index, verify the preview, and save; restart the engine when requested. An index can change after reconnecting devices, so confirm the actual camera after reboot. Do not run the terminal and dashboard against the same camera/catalog/database concurrently.

For a terminal session, verified CLI options allow a lighter capture configuration:

```sh
venv/bin/python main.py --source 0 --width 1280 --height 720 --fps 15 --scale 0.5
```

The terminal does not provide the dashboard login boundary: run it only from an appropriately restricted macOS account. Tune frame rate on the real camera; requested resolution/FPS are backend-dependent. Lowering FPS reduces capture/render work; face quality, recognition thresholds, and attendance verification still need real-world acceptance.

## Power and session lifecycle

Use stable AC power, a reliable USB connection, and a ventilated location. Test a powered hub if one is required; do not assume that a hub or camera honors timeout settings.

On macOS, the dashboard attendance engine and the license plate worker acquire an idle-system-sleep assertion while running and release it when stopped or shut down. This uses `/usr/bin/caffeinate -i -w <application PID>`; process exit also releases its assertion. Stopped dashboard screens do not permanently change system power settings. If either monitor remains running, its assertion can remain active. Check the assertion and verify real capture on the deployment Mac, rather than assuming that an open dashboard is actively monitoring.

Idle sleep prevention does not keep the display lit; the Mac can lock or turn off its display. It does not guarantee operation through a lid close, forced sleep, loss of power, or a macOS reboot. Screen locking is separate from system sleep and should stay enabled. The application makes no global macOS power-setting changes.

For a standalone terminal monitoring session, scope idle sleep prevention to its process lifetime:

```sh
/usr/bin/caffeinate -i venv/bin/python main.py --source 0 --fps 15
```

This assertion ends when the terminal process exits. Do not wrap a stopped dashboard in an extra long-lived `caffeinate` process: that extra assertion would survive pressing Stop in the application.

After sleep/wake, confirm the camera and attendance state before resuming. A stopped or paused engine is not recording attendance. A window remaining open is not proof that frames, recognition, and persistence are healthy.

## Sign-in and optional login launch

Create individual accounts in Users & Access, give operators only the permissions they need, and retain an administrator recovery account. A deployment with no users and no PIN opens without a dashboard sign-in boundary. A remembered session deliberately reduces interactive login requirements; reserve it for a dedicated, physically secured operator account. Do not enable macOS automatic login just to avoid the application's sign-in flow. Verify Camera permission for the actual launcher in macOS Privacy & Security before unattended operation; a LaunchAgent cannot grant it.

A user LaunchAgent can optionally start the dashboard after that user logs in. It does not start before login or unlock FileVault. The following is a **reviewable example, not an installed service**. No agent is created or enabled by this guide. Adapt paths if the checkout moves. Do not add secrets to its arguments or logs.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>local.sv-face-id.dashboard</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/timdev/Desktop/works/Python Projects/sv-face-recognition/venv/bin/python</string>
    <string>/Users/timdev/Desktop/works/Python Projects/sv-face-recognition/dashboard.py</string>
    <string>--settings</string>
    <string>/Users/timdev/Desktop/works/Python Projects/sv-face-recognition/settings.json</string>
    <string>--start</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/timdev/Desktop/works/Python Projects/sv-face-recognition</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
</dict>
</plist>
```

If an operator chooses to save this as `~/Library/LaunchAgents/local.sv-face-id.dashboard.plist`, validate and manage it with:

```sh
plutil -lint "$HOME/Library/LaunchAgents/local.sv-face-id.dashboard.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.sv-face-id.dashboard.plist"
launchctl print "gui/$(id -u)/local.sv-face-id.dashboard"
launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.sv-face-id.dashboard.plist"
```

`bootstrap` enables a real service: run it only after choosing that deployment policy. `bootout` stops/unloads it and is the first step before maintenance. The example retries unsuccessful process exits, with throttling; an intentional successful Quit does not immediately relaunch. It does not detect a hung process, stopped engine, failed camera, failed database, or uncompleted sign-in. The LaunchAgent does not create a sleep assertion itself; the running dashboard engines manage their own assertions. Validate reboot/login behavior and prevent duplicate launchers before relying on it.

## Disk, evidence, and backup privacy

Optional plate OCR now runs in a separate environment, keeping one OpenCV wheel
in each environment. The desktop uses regular OpenCV; the OCR child uses headless
OpenCV. From the checkout:

```sh
python3.12 -m venv .venv-plates
.venv-plates/bin/python -m pip install -r requirements-plates.txt
venv/bin/python -m pip check
.venv-plates/bin/python -m pip check
```

The source checkout discovers `.venv-plates` automatically. If installed elsewhere,
launch with `SV_PLATE_PYTHON` set to its absolute Python executable. Do not install
plate requirements into the desktop `venv`. OCR has bounded request/response sizes,
a 120-second model-start deadline, and a 15-second inference deadline. A timed-out
child is terminated; the UI reports the failure and the operator can restart OCR.
Model downloads require initial network access. A packaged application needs an
external plate environment and `SV_PLATE_PYTHON`; the child script is included as data.

Remembered desktop sign-ins expire after 30 days on the next startup. They do not
interrupt a running attendance session. New passwords and PINs use salted
PBKDF2-SHA256; legacy account hashes upgrade after successful sign-in. Existing
SHA256 PINs remain readable until a new PIN is saved. OS account/file permissions
remain the trust boundary for a remembered desktop sign-in file.

Attendance evidence and plate captures are sensitive personal data. In a source run, defaults live under the checkout; a packaged macOS build defaults to `~/Library/Application Support/SV Face ID`. Settings may point elsewhere. Inspect actual configured database, capture, catalog, and output paths before backing up or clearing anything.

- Watch free disk space and daily growth. Use `df -h .` and `du -sh captures plate_captures enrollment_photos logs` from the checkout for default directories that exist. Keep substantial operating headroom; the face snapshot service's 100 MiB low-space check is a last-resort safeguard, not a capacity plan or a plate-capture safeguard.
- There is no automatic age-based retention for plate images or `captures.jsonl`. The UI keeping only 200 history rows does not delete evidence. Face evidence, logs, and database growth also require an explicit retention policy. No production data was purged by the hardening work.
- Decide the approved evidence period, backup period, and deletion responsibility before deployment. Face evidence can have a portrait plus `captures/context` image and metadata companions. Preserve/delete related files coherently. Plate records reference full-frame and crop images; removing only images leaves broken history references. Do not use an unreviewed recursive deletion command against production evidence.
- Encrypt backup storage and restrict access to the operator/administrator group. Include enrollment templates, `employees.json`, enrollment photos, configured attendance database, and required evidence. Treat settings, `.session`, `.accounts`, `.mobile_api.key`, camera URLs, and database credentials as secrets. Do not attach them to support tickets or copy them to a public repository. Consider whether session/key material should be excluded from ordinary backups and reprovisioned on restore.
- Stop monitoring cleanly before a filesystem backup, then verify it restarts. For a live SQLite backup use SQLite's backup mechanism rather than copying only `attendance.db` while its WAL is active. Remote database deployments need their own database-native consistent backup. Test restoration into an isolated directory/account before depending on it; preserve employee-ID mappings and sample-photo references together.
- Template loading now restricts pickle globals to NumPy numeric compatibility primitives and rejects object arrays, with a 256 MiB input limit. Import only approved employee exports/catalogs. This restriction does not make arbitrary external files safe against every allocation/resource-exhaustion attack.

Camera URL passwords and query strings are redacted for display/capture metadata through the shared source formatter. Old records and backups may still contain earlier unredacted values; treat them accordingly. Rotating a compromised camera password remains necessary even after a UI redaction fix.

## Acceptance before unattended use

Use test employees and a disposable capture destination for destructive/failure scenarios. Record time, camera model/USB topology, macOS version, settings, and outcome.

| Scenario | Required observation |
| --- | --- |
| USB unplug during capture | Preview stops showing fresh detections; disconnected/reconnecting state appears; no attendance is confirmed from stale frames. UI stays usable. |
| Replug same camera | Fresh frames resume after retries; verify camera index and view. New camera generation invalidates old recognition/plate confirmation. Confirm only one active camera owner. |
| Camera opens but supplies no frames | Reconnect attempts back off up to 30 seconds; no tight retry loop. Recovery after replug remains possible. |
| Database unavailable or disk full | Operator sees failure/pending state; no false success. Restore capacity/connectivity, verify saved rows and evidence, and reconcile failed captures. Do not simulate full disk on production storage. |
| Stop/start and detached monitors | Repeated cycles and native window close/redock preserve the shared engine, do not multiply camera workers, and keep reports usable. Fullscreen stays confined to the selected window. |
| Screen lock / display off | Check that capture and persistence continue if required by the deployment, while access to the desktop remains locked. |
| Explicit sleep/wake | Expect an attendance gap during sleep. After wake verify real fresh frames, reconnect, and resumed persistence; restart if the driver does not return. |
| Reboot and login | No monitoring is assumed before login. Camera permission, authentication, camera selection, optional LaunchAgent, and `--start` behave as configured. |
| Permission revoked / account disabled | Sensitive windows redock/hide or sign-out occurs; no detached window retains unauthorized access. |
| 24-hour soak | Track memory, CPU, disk growth, temperatures, fresh-frame age, database rows, and alerts. Include representative enrollment count and both monitors if used. Check several known attendance events, not just an open window. |

Camera and recognition shutdown use bounded waits. Some native USB drivers/model calls can ignore timeouts; they cannot safely be force-killed inside the process. A driver/backend that remains stuck may require restarting the application or reconnecting the device. Automatic process restart alone will not resolve every hardware failure, and monitoring should notify an operator when attendance cannot be trusted.

Current regression evidence includes camera reconnect/release-failure tests, preview freshness and stale plate rejection, bounded plate memory, safe metadata, detached window lifecycle/access tests, and restricted template-loader/transfer/enrollment tests. These synthetic/offscreen tests establish software behavior under their tested conditions; they do not replace the acceptance table or the missing physical 24-hour soak.

### Repeatable soak harness

The harness uses temporary empty enrollment/catalog/database paths, never the
production employee data. It saves aggregate metrics only. Choose and verify the
camera index before running; index 0 can be the Mac's built-in camera rather than
the USB camera. A camera name inventory and a short live preview should agree with
the intended hardware. Do not run it while the production app owns that device.

```sh
# Worker/reconnect smoke test without a physical camera:
venv/bin/python scripts/soak_runtime.py --seconds 65 --sample-seconds 5 --report /tmp/sv-synthetic-soak.json

# Physical camera soak, after confirming the USB camera's index:
venv/bin/python scripts/soak_runtime.py --camera 0 --seconds 86400 --width 1280 --height 720 --fps 30 --report /tmp/sv-camera-24h.json
```

Reports include status, elapsed time, source hashes, PID, memory samples, preview
counts, camera availability, queue depths, errors, and shutdown status. Samples
are bounded to 1,440 entries. A camera outage exceeding 60 seconds, storage outage,
dead worker, engine error, or memory growth over 100 MiB after warmup fails the run.
Requested camera resolution/FPS are best effort. Interrupt with Ctrl+C to release
the device and idle-sleep assertion; interrupted runs do not count as a pass.
An empty-catalog soak exercises camera/detection/coordination, not representative
enrolled attendance throughput, license-plate accuracy, or application UI layout.
Use the acceptance matrix separately with representative employees and both
monitors where required. A successful 24-hour run is evidence for that hardware
and test configuration; it is not an unconditional uptime guarantee.
