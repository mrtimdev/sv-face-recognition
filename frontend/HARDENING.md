# Production hardening and upgrade notes

## Attendance delivery

New events retain the HRM URL configured when the engine started. URLs are
normalized, including default ports. Another hostname, port, scheme or base
path is a different destination: we do not assume that two servers with the
same API key or ping response are the same organization. Changing settings or
automatic office/internet failover cannot reroute an old batch. Restart the
engine after intentionally selecting a new destination.

This conservative binding also holds events when office and internet URLs
refer to the same HRM installation. Review and explicitly reassign those events
after verifying the destination. Authenticated stable installation identity
across aliases is a future backend protocol extension.

Legacy pending events have no trustworthy destination and remain unassigned.
Historical acknowledgements appear as `legacy_resolved`, not delivered, because
the old schema could not distinguish rejection from successful delivery.
New HRM rejections retain their own status and a reason; reports show delivered,
rejected, pending, unassigned and legacy counts. The outbox tooltip shows up to
100 items; the maintenance command lists up to 1,000.

From `frontend/`, review first:

```sh
venv/bin/python scripts/maintain_data.py --settings settings.json outbox
```

With monitoring stopped, explicitly assign one reviewed event. An empty
`--from-destination` means an unassigned legacy event. Use the exact old URL for
an already assigned event. `--retry-rejected` is required to retry a rejection.
Delivered/legacy-resolved events cannot be silently requeued by this command.

```sh
venv/bin/python scripts/maintain_data.py --settings settings.json assign \
  --event EVENT_ID --from-destination '' --to https://your-hrm.example --stopped
```

## Live Monitor and sync timing

Face detection, identity matching, expression/liveness checks and anti-spoofing
run on the local device with bundled models. Live Monitor applies the configured
OpenCV thread limit, uses the latest camera/recognition data rather than an
unbounded frame backlog, and stages evidence outside its coordination loop.
With the default SQLite backend, attendance storage and the delivery outbox are
local. Selecting a remote attendance database makes that database a dependency
for recording; choose SQLite for an offline terminal.

The dashboard starts an upload immediately after a local attendance save. It
also checks the outbox every five seconds and immediately on HRM reconnection.
An acknowledgement timeout of 30 seconds causes retransmission without waiting
for the next periodic tick. Batches contain at most 20 events. Photos are resized
and encoded on the upload worker; no continuous camera video is sent to HRM.
The same event ID is retained on retries and an HRM acknowledgement is required
to mark delivery. No hourly/nightly sync schedule is needed or configured.

System Status shows backend sync separately from local recording. A successful
local attendance card does not mean HRM has already acknowledged it. Offline
captures wait locally and sync resumes when the dashboard is open and HRM is
reachable. Pause/Stop stops recognition but does not stop the dashboard's uploader;
quitting the dashboard ends automatic uploads until it is reopened. The standalone
OpenCV terminal entry point does not run the Qt dashboard uploader.

## Recovery and backup

Accepted jobs are committed to `attendance.db.pending.sqlite` (or the configured
database path plus `.pending.sqlite`) before submit returns success. The spool
holds at most the configured persistence queue capacity and retains original
event IDs, destinations and lossless evidence. Errors retain jobs for retry;
saved, duplicate and blocked outcomes remove them. A failure after the main
database commits but before spool removal safely replays the same event ID.
Live Monitor queues eligible evidence to a bounded background staging worker,
so spool serialization/fsync does not block preview or liveness coordination.
A staging request remains pending until the durable commit; only then does the
capture indicator fire. Attendance success still requires the main database
commit. Pending requests that have not reached the spool are not crash-durable.
Shutdown drains staging before persistence and refuses restart while either
worker remains stuck. Queue counters are cached rather than querying/locking
the disk at preview frequency. Disk loss still requires backups.

Enrollment metadata uses private `.lock.sqlite` and `.recovery.sqlite` sidecars
next to the employee mapping. The undo journal precedes file replacement;
desktop/CLI/import writers and cooperating catalog readers use the same lock.
Interrupted publication is rolled back before the next cooperating access.
Referenced sample deletion is deferred until metadata commits. A crash can
leave unreferenced new photos, which are preserved rather than guessed at and
deleted. External scripts must use the enrollment service rather than editing
catalog files directly. Do not remove a nonempty recovery journal.

Stop monitoring and enrollment before backup. The following command creates a
new directory, uses SQLite's backup API, includes the spool, enrollment metadata,
sample photos and attendance captures, and verifies hashes and database integrity.
It excludes credentials, settings, plate evidence and external HRM data; back
those up separately under their own policies. Remote attendance databases need
database-native backups. Keep the backup directory private and encrypted.

```sh
venv/bin/python scripts/maintain_data.py --settings settings.json backup \
  --output /path/to/new-private-backup --stopped
venv/bin/python scripts/maintain_data.py verify-backup /path/to/new-private-backup
```

For restoration, stop the application, verify the backup, and restore files to
the locations recorded in `manifest.json`. Restore the spool alongside the main
database. Outbox payloads contain original evidence paths, so restoring to new
paths requires deliberate path migration before uploads resume. Reprovision
settings/credentials separately. Exercise this in an isolated environment before
relying on production recovery.

## Evidence retention

Preview first; no age-based deletion runs automatically. Only evidence belonging
to delivered events is eligible. Pending, rejected and legacy-resolved evidence
is retained for reconciliation; attendance history itself is retained.

```sh
venv/bin/python scripts/maintain_data.py --settings settings.json retention --days 90
# After reviewing the list and stopping monitoring:
venv/bin/python scripts/maintain_data.py --settings settings.json retention --days 90 --apply --stopped
```

The command removes the portrait, context image and metadata together and clears
their references. Plate/unknown-face evidence still needs a separate policy.

## Deployment and verification

Desktop releases depend on the regression workflow. The separate `svhrm`
repository also runs Maven tests before packaging/deployment and has a PR test
workflow. Configure repository branch protection to require these checks.

The container health check now requires a fresh engine heartbeat, recent camera
frames, active recognition/persistence workers, available storage and queue
capacity. A signed-out, paused or stopped engine is unhealthy. Docker's health
status alone does not restart an unhealthy container; connect it to monitoring.

The physical-camera 24-hour soak, deployment-camera recognition/replay calibration,
sleep/wake, actual disk-full and power-cut acceptance remain required. Synthetic
tests cannot certify those hardware conditions. Use OPERATIONS.md and the soak
harness with the intended camera and representative employees.

## Removed Flutter integration

Removed the Python mobile enrollment API, guided mobile pose implementation,
launcher, mobile requirements, API guide and corresponding tests. The setup wizard
now configures HRM rather than creating a mobile administrator. Docker installs
only desktop dependencies. The separate Flutter repository was not changed and
its Python enrollment endpoint is no longer supplied here. Existing mobile
databases, keys and model caches are not deleted; their ignore rules remain so
sensitive historical data cannot accidentally be committed.

## Validation for this change

All 73 Python test modules passed when run in isolated processes. Isolation
prevents Qt application/global state from leaking between modules. The aggregated
JUnit reports contain 1,050 cases, including subtests and imported test classes;
this is not a count of distinct scenarios. Dependency consistency and whitespace
checks passed. The separate svhrm suite passed 1,103 Java tests and 75 JavaScript
tests; stale JavaScript mocks were updated for the existing report APIs.

The 65-second synthetic soak observed 1,770 camera frames and 1,780 preview
updates, exercised reconnect, reported no engine errors, and stopped every
worker. It used temporary storage and no production enrollment data. This does
not replace the physical acceptance work described above.
