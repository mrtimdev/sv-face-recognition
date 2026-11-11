#!/usr/bin/env python3
"""Review/reconcile delivery, back up local data, or preview evidence retention."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from face_attendance.settings import Settings
from face_attendance.repository import AttendanceRepository
from face_attendance.report import AttendanceReader
from face_attendance.maintenance import backup, verify_backup, reassign_event, purge_evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('outbox')
    assign = sub.add_parser('assign')
    assign.add_argument('--event', required=True)
    assign.add_argument('--from-destination', default='')
    assign.add_argument('--to', required=True)
    assign.add_argument('--retry-rejected', action='store_true')
    assign.add_argument('--stopped', action='store_true', required=True, help='Confirm monitoring and enrollment are stopped')
    retention = sub.add_parser('retention')
    retention.add_argument('--days', type=int, required=True)
    retention.add_argument('--apply', action='store_true')
    retention.add_argument('--stopped', action='store_true')
    save = sub.add_parser('backup')
    save.add_argument('--output', type=Path, required=True)
    save.add_argument('--stopped', action='store_true', required=True)
    verify = sub.add_parser('verify-backup')
    verify.add_argument('directory', type=Path)
    args = parser.parse_args()
    if args.command == 'verify-backup':
        verify_backup(args.directory)
        print('Backup checksums and SQLite integrity verified')
        return
    config = Settings.load(args.settings).to_config()
    if args.command == 'backup':
        backup(config, args.output)
        print(f'Backup verified: {args.output}')
        return
    if args.command == 'outbox':
        reader = AttendanceReader(config)
        try:
            print(json.dumps({'counts': reader.outbox_counts(), 'events': reader.delivery_issues(1000)}, indent=2))
        finally:
            reader.close()
        return
    if args.command == 'retention' and (args.days < 1 or (args.apply and not args.stopped)):
        parser.error('Use positive --days; applying retention also requires --stopped')
    repo = AttendanceRepository(config)
    try:
        if args.command == 'assign':
            reassign_event(repo, args.event, args.from_destination, args.to, args.retry_rejected)
            print('Event reassigned; delivery resumes when monitoring restarts')
        else:
            rows = purge_evidence(repo, config.capture_dir, time.time() - args.days * 86400, args.apply)
            print(json.dumps({'applied': args.apply, 'events': rows}, indent=2))
    finally:
        repo.close()


if __name__ == '__main__':
    main()
