"""Cross-process catalog serialization and durable undo journal.

The journal is committed before each file replacement. The next cooperating
reader/writer rolls back interrupted publication before using the catalog.
Unreferenced newly-created photos may remain after a crash; old referenced
photos are never deleted before the metadata transaction commits.
"""
from contextlib import contextmanager
from pathlib import Path
import os
import errno
import sqlite3
import tempfile
import threading
import inspect
from functools import wraps

_local = threading.local()
_mutex = threading.RLock()


def catalog_guard(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        bound = inspect.signature(method).bind(*args, **kwargs).arguments
        employees = Path(bound.get('employees_path', bound.get('path')))
        encodings = bound.get('encodings_path')
        if not employees.exists() and not Path(str(employees) + '.recovery.sqlite').exists() and (
                encodings is None or not Path(encodings).exists()):
            return method(*args, **kwargs)
        with catalog_transaction(encodings, employees):
            return method(*args, **kwargs)
    return guarded


def sync_directory(path):
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EBADF, errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(fd)


def restore(path, content):
    path = Path(path)
    if content is None:
        path.unlink(missing_ok=True)
    else:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    sync_directory(path.parent)


def before_write(path):
    state = getattr(_local, 'state', None)
    if state is None:
        return
    path = Path(path).resolve()
    # Only metadata uses _atomic_write. New photo files need no undo copy.
    if path not in state['paths']:
        return
    with state['journal']:
        state['journal'].execute('INSERT OR IGNORE INTO undo(path, content) VALUES (?, ?)',
                                (str(path), path.read_bytes() if path.exists() else None))


def after_commit(callback):
    state = getattr(_local, 'state', None)
    if state is None:
        callback()
    else:
        state['cleanup'].append(callback)


def _recover(journal):
    for path, content in journal.execute('SELECT path, content FROM undo'):
        restore(path, content)


def _clear(journal):
    with journal:
        journal.execute('DELETE FROM undo')


@contextmanager
def catalog_transaction(encodings_path, employees_path):
    employees_path = Path(employees_path).resolve()
    paths = {employees_path}
    if encodings_path is not None:
        paths.add(Path(encodings_path).resolve())
    with _mutex:
        current = getattr(_local, 'state', None)
        if current is not None:
            if current['owner'] != employees_path:
                raise RuntimeError('Cannot nest transactions for different employee catalogs')
            current['paths'].update(paths)
            yield current
            return
        employees_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(employees_path) + '.lock.sqlite'
        journal_path = str(employees_path) + '.recovery.sqlite'
        for path in (lock_path, journal_path):
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        lock = sqlite3.connect(lock_path, timeout=30)
        journal = None
        try:
            lock.execute('BEGIN EXCLUSIVE')
            journal = sqlite3.connect(journal_path)
            journal.execute('PRAGMA synchronous=FULL')
            journal.execute('PRAGMA secure_delete=ON')
            journal.execute('CREATE TABLE IF NOT EXISTS undo(path TEXT PRIMARY KEY, content BLOB)')
            journal.commit()
            # This also recovers writes interrupted by SIGKILL/power loss.
            _recover(journal)
            _clear(journal)
            state = {'owner': employees_path, 'paths': paths, 'journal': journal,
                     'cleanup': [], 'rollback': False}
            _local.state = state
            try:
                yield state
            except BaseException:
                state['rollback'] = True
                raise
            finally:
                _local.state = None
                if state['rollback']:
                    _recover(journal)
                _clear(journal)
                if not state['rollback']:
                    for callback in state['cleanup']:
                        callback()
        finally:
            if journal is not None:
                journal.close()
            lock.close()
