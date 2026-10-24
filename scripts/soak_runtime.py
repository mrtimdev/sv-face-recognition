#!/usr/bin/env python3
"""Exercise the desktop workers in temporary storage and write bounded metrics.

Default: 24 hours of synthetic frames, including periodic camera disconnects.
Use --camera 0 for a real USB camera and the real recognition backend. No
production catalog/settings are loaded and no employee templates are enrolled.
"""
import argparse
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def validate_runtime(engine, errors, require_storage):
    if not engine.running or any(not worker.thread.is_alive() for worker in
                                (engine.camera, engine.recognition, engine.persistence)):
        raise RuntimeError('A required runtime worker stopped')
    if errors:
        raise RuntimeError('Engine reported a runtime error')
    if require_storage and not engine.stats.get('storage_ready'):
        raise RuntimeError('Attendance storage is unavailable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=86400)
    parser.add_argument('--camera', type=int, help='Real USB camera index; otherwise synthetic frames')
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--fps', type=float, default=30)
    parser.add_argument('--max-outage-seconds', type=float, default=60)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--sample-seconds', type=float, default=30)
    parser.add_argument('--max-growth-mb', type=float, default=100)
    args = parser.parse_args()
    if not all(0 < value < float('inf') for value in
               (args.seconds, args.sample_seconds, args.max_growth_mb, args.fps, args.max_outage_seconds)):
        parser.error('Durations and memory limit must be finite and positive')
    if not 80 <= args.width <= 4096 or not 80 <= args.height <= 4096:
        parser.error('Camera width and height must be between 80 and 4096')

    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    import numpy as np
    import psutil
    from PyQt6.QtWidgets import QApplication
    from face_attendance.dashboard.engine import AttendanceEngine
    from face_attendance.settings import Settings

    class EmptyBackend:
        def face_locations(self, image, model=None):
            return []

    class SyntheticCapture:
        def __init__(self):
            self.reads = 0
            self.frame = np.zeros((240, 320, 3), np.uint8)

        def isOpened(self):
            return True

        def read(self):
            time.sleep(1 / 30)
            self.reads += 1
            return (False, None) if self.reads >= 900 else (True, self.frame)

        def release(self):
            pass

    app = QApplication.instance() or QApplication([])
    process = psutil.Process()
    samples, errors = deque(maxlen=1440), deque(maxlen=10)
    report = {'mode': 'camera-real-backend' if args.camera is not None else 'synthetic',
              'camera_index': args.camera,
              'requested_seconds': args.seconds, 'status': 'running',
              'pid': os.getpid(), 'started_at': datetime.now(timezone.utc).isoformat(),
              'source_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for folder in ('face_attendance', 'license_plate')
                                for path in sorted((ROOT / folder).rglob('*.py'))},
              'production_data_used': False, 'preview_updates': 0,
              'connected_samples': 0, 'reconnecting_samples': 0}
    started, baseline_rss = time.monotonic(), None

    def write_report():
        report.update(elapsed_seconds=round(time.monotonic() - started, 2),
                      samples=list(samples), errors=list(errors))
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile('w', dir=args.report.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(report, handle, indent=2, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        try:
            os.replace(temporary, args.report)
        finally:
            temporary.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix='sv-soak-') as directory:
        root = Path(directory)
        settings = replace(Settings(), source=args.camera if args.camera is not None else 0,
                           camera_width=args.width, camera_height=args.height, target_fps=args.fps,
                           encodings_path=str(root / 'empty_sface.pickle'),
                           employees_path=str(root / 'employees.json'),
                           db_path=str(root / 'attendance.db'),
                           capture_dir=str(root / 'captures'),
                           log_path=str(root / 'observations.csv'))
        engine = AttendanceEngine(settings, use_lock=False,
                                  backend=EmptyBackend() if args.camera is None else None,
                                  capture_factory=SyntheticCapture if args.camera is None else None)
        def receive_frame(frame):
            report['preview_updates'] += 1
        engine.frameReady.connect(receive_frame)
        engine.errorRaised.connect(errors.append)
        next_sample, last_sequence, received_camera_frames = 0., -1, 0
        last_connected = started
        try:
            engine.start()
            while time.monotonic() - started < args.seconds:
                app.processEvents()
                elapsed = time.monotonic() - started
                if engine.camera.status(time.monotonic()) == 'CONNECTED':
                    last_connected = time.monotonic()
                if time.monotonic() - last_connected > args.max_outage_seconds:
                    raise RuntimeError('Camera outage exceeded the configured soak limit')
                packet = engine.camera.frames.get()
                if packet is not None and packet.sequence != last_sequence:
                    received_camera_frames += 1
                    last_sequence = packet.sequence
                if elapsed >= next_sample:
                    rss = process.memory_info().rss / 1024 ** 2
                    if baseline_rss is None and elapsed >= min(30, args.seconds / 3):
                        baseline_rss = rss
                    status = engine.camera.status(time.monotonic())
                    report['connected_samples' if status == 'CONNECTED' else 'reconnecting_samples'] += 1
                    samples.append(dict(seconds=round(elapsed, 2), rss_mb=round(rss, 2),
                                        python_threads=threading.active_count(), camera=status,
                                        jobs=engine.persistence.jobs.qsize(),
                                        results=engine.persistence.results.qsize()))
                    if baseline_rss is not None and rss - baseline_rss > args.max_growth_mb:
                        raise RuntimeError('Memory growth exceeded the configured soak limit')
                    validate_runtime(engine, errors, require_storage=elapsed > 15)
                    next_sample = elapsed + args.sample_seconds
                    write_report()
                time.sleep(.01)
            if not received_camera_frames:
                raise RuntimeError('No camera frames received; this is not a valid camera soak')
            app.processEvents()
            validate_runtime(engine, errors, require_storage=True)
            report['status'] = 'completed'
        except KeyboardInterrupt:
            report['status'] = 'interrupted'
        except Exception as exc:
            errors.append(str(exc))
            report['status'] = 'failed'
        finally:
            engine.shutdown()
            app.processEvents()
            report['camera_frames_observed'] = received_camera_frames
            report['workers_stopped'] = all(not worker.thread.is_alive() for worker in
                                            (engine.camera, engine.recognition, engine.persistence))
            if not report['workers_stopped']:
                report['status'] = 'failed'
            write_report()
    print(json.dumps({key: value for key, value in report.items() if key not in ('samples', 'source_sha256')}))
    return 0 if report['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
