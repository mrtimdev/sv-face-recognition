"""One engine per data directory.

The advisory lock stops a second dashboard from opening the same camera and
database. The OS releases it automatically if the process dies, and SQLite
keeps its own cooldown authority regardless. On platforms without ``fcntl``
the lock is simply unavailable and the SQLite rules still apply.
"""
import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class EngineLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    @property
    def held(self):
        return self.handle is not None

    def acquire(self):
        if self.handle is not None:
            return True
        if fcntl is None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise RuntimeError(
                f"Another attendance engine is already using {self.path.parent}. "
                "Close the other dashboard or terminal before starting a second one.") from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self.handle = handle
        return True

    def release(self):
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
