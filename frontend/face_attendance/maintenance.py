"""Explicit, reviewable outbox reconciliation, evidence retention and backups."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

from .catalog_transaction import catalog_transaction
from .destination import destination_id
from .storage import delete_capture


def reassign_event(repository, event_id, expected_destination, new_destination, retry_rejected=False):
    destination = destination_id(new_destination)
    if not destination:
        raise ValueError('A destination is required')
    with repository.connection as db:
        row = db.execute('SELECT destination, delivery_status FROM attendance_outbox WHERE event_id=?',
                         (event_id,)).fetchone()
        if row is None or (row['destination'] or '') != expected_destination:
            raise ValueError('Event destination changed or event does not exist; review again')
        allowed = ('pending', 'rejected') if retry_rejected else ('pending',)
        if row['delivery_status'] not in allowed:
            raise ValueError('Event is not pending; rejected retries require --retry-rejected')
        db.execute("UPDATE attendance_outbox SET destination=?, delivery_status='pending', "
                   "delivery_reason=NULL, resolved_at=NULL, synced_at=NULL WHERE event_id=?",
                   (destination, event_id))
        db.execute('INSERT INTO audit_log(timestamp, action, detail, actor) VALUES (?, ?, ?, ?)',
                   (datetime.now(timezone.utc).isoformat(),
                    'outbox_reassigned', json.dumps({'eventId': event_id, 'from': expected_destination,
                                                    'to': destination, 'retryRejected': retry_rejected}), 'maintenance'))


def retention_candidates(repository, before_epoch):
    # Never discard evidence still needed for upload, rejection review, or legacy reconciliation.
    return [dict(row) for row in repository.connection.execute(
        "SELECT a.event_id, a.snapshot FROM attendance a JOIN attendance_outbox o ON a.event_id=o.event_id "
        "WHERE o.delivery_status='delivered' AND a.captured_at_epoch < ? AND a.snapshot IS NOT NULL AND a.snapshot<>''",
        (before_epoch,))]


def purge_evidence(repository, capture_dir, before_epoch, apply=False):
    candidates = retention_candidates(repository, before_epoch)
    if apply:
        for row in candidates:
            delete_capture(row['snapshot'], capture_dir)
            with repository.connection as db:
                db.execute("UPDATE attendance SET snapshot='' WHERE event_id=?", (row['event_id'],))
                payload = db.execute('SELECT payload FROM attendance_outbox WHERE event_id=?', (row['event_id'],)).fetchone()
                data = json.loads(payload[0])
                data['snapshotPath'] = ''
                db.execute('UPDATE attendance_outbox SET payload=? WHERE event_id=?', (json.dumps(data), row['event_id']))
        repository.audit('evidence_retention', f'Removed evidence for {len(candidates)} delivered events', 'maintenance')
    return candidates


def backup(config, destination):
    """Call only with monitoring/enrollment stopped. Secrets are intentionally excluded."""
    if config.db_backend != 'sqlite':
        raise ValueError('Use database-native backup for remote databases')
    destination = Path(destination).resolve()
    sources = {'attendance.db': config.db_path, 'pending.sqlite': Path(str(config.db_path) + '.pending.sqlite'),
               'employees.json': config.employees_path, 'templates.pickle': config.encodings_path,
               'captures': config.capture_dir, 'enrollment_photos': config.encodings_path.parent / 'enrollment_photos'}
    for source in sources.values():
        if destination == source.resolve() or source.resolve() in destination.parents:
            raise ValueError('Backup destination must be outside the source data')
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    with catalog_transaction(config.encodings_path, config.employees_path):
        for name, source in sources.items():
            if not source.exists():
                continue
            target = destination / name
            if source.is_symlink() or (source.is_dir() and any(p.is_symlink() for p in source.rglob('*'))):
                raise ValueError('Backup source contains symbolic links')
            if name in ('attendance.db', 'pending.sqlite'):
                original = sqlite3.connect(f'{source.resolve().as_uri()}?mode=ro', uri=True)
                copied = sqlite3.connect(target)
                try:
                    original.backup(copied)
                finally:
                    copied.close()
                    original.close()
            elif source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        manifest = {'version': 1, 'original_paths': {k: str(v.resolve()) for k, v in sources.items()},
                    'files': {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in destination.rglob('*') if p.is_file()}}
        (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    verify_backup(destination)
    return manifest


def verify_backup(destination):
    destination = Path(destination).resolve()
    manifest = json.loads((destination / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = (destination / name).resolve()
        if destination not in path.parents or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f'Backup verification failed: {name}')
    for name in ('attendance.db', 'pending.sqlite'):
        path = destination / name
        if path.exists():
            db = sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError(f'Invalid backup database: {name}')
            finally:
                db.close()
    return True
