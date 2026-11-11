"""Bounded local attendance spool. Acceptance follows a FULL SQLite commit.

Frames use NumPy's non-object format (never pickle). A row stays in the spool
until the attendance repository reports a terminal outcome; replay keeps the
original event ID and destination, including after power/process failure.
"""
from contextlib import contextmanager
from dataclasses import fields
import io
import json
import os
from pathlib import Path
import queue
import sqlite3
import threading

import numpy as np
from .models import CaptureJob


def encode_frame(frame):
    if frame is None:
        return None
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.ndim != 3:
        raise ValueError('Attendance spool requires an 8-bit image')
    stream = io.BytesIO()
    np.save(stream, frame, allow_pickle=False)
    return stream.getvalue()


def decode_frame(blob):
    return None if blob is None else np.load(io.BytesIO(blob), allow_pickle=False)


class DurableJobs:
    def __init__(self, path, maxsize):
        self.path, self.maxsize = Path(path), maxsize
        self._lock = threading.RLock()
        self._active = None
        self._size = 0
        if self.path.exists():
            db = sqlite3.connect(f'{self.path.resolve().as_uri()}?mode=ro', uri=True, timeout=5)
            try:
                self._size = db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]
            finally:
                db.close()

    @contextmanager
    def _database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(descriptor)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON')
            db.execute('CREATE TABLE IF NOT EXISTS jobs(event_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, frame BLOB NOT NULL, context BLOB)')
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()

    def put_nowait(self, job):
        metadata = {field.name: getattr(job, field.name) for field in fields(job)
                    if field.name not in ('frame', 'context_frame')}
        frame, context = encode_frame(job.frame), encode_frame(job.context_frame)
        with self._lock:
            with self._database() as db:
                if db.execute('SELECT 1 FROM jobs WHERE event_id=?', (job.event_id,)).fetchone():
                    return
                if db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] >= self.maxsize:
                    raise queue.Full
                db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?)',
                           (job.event_id, json.dumps(metadata, allow_nan=False), frame, context))
            self._size += 1

    def get_nowait(self):
        with self._lock:
            if self._active is not None or not self.path.exists():
                raise queue.Empty
            with self._database() as db:
                row = db.execute('SELECT event_id, metadata, frame, context FROM jobs ORDER BY rowid LIMIT 1').fetchone()
            if row is None:
                raise queue.Empty
            metadata = json.loads(row[1])
            metadata['from_spool'] = True
            job = CaptureJob(**metadata, frame=decode_frame(row[2]), context_frame=decode_frame(row[3]))
            self._active = row[0]
            return job

    def task_done(self):
        with self._lock:
            with self._database() as db:
                removed = db.execute('DELETE FROM jobs WHERE event_id=?', (self._active,)).rowcount
            self._size = max(0, self._size - removed)
            self._active = None

    def retry(self):
        with self._lock:
            self._active = None

    def qsize(self):
        # Monitoring reads this at preview frequency. Never wait on a spool fsync
        # or read images/the database merely to render a queue counter.
        return self._size

    def empty(self):
        return self.qsize() == 0

    @property
    def unfinished_tasks(self):
        return self.qsize()
