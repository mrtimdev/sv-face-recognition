"""A one-slot mailbox: slow consumers never build up a video backlog."""
import threading


class LatestValue:
    def __init__(self):
        self._value = None
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._version = 0
        self.ready = threading.Event()

    def put(self, value):
        with self._lock:
            self._value = value
            self._version += 1
            self.ready.set()
            self._changed.notify_all()

    @property
    def version(self):
        """Counts ``put`` calls; compare it to see whether a newer value arrived."""
        with self._lock:
            return self._version

    def wait_newer(self, version, timeout):
        """Sleep until a value newer than *version* is put, or *timeout* passes.

        Returns the current version. Lets a consumer react the moment a frame
        lands instead of sleeping a fixed interval and showing it late.
        """
        with self._changed:
            self._changed.wait_for(lambda: self._version != version, max(0.0, timeout))
            return self._version

    def get(self):
        with self._lock:
            return self._value

    def take(self):
        with self._lock:
            value, self._value = self._value, None
            self.ready.clear()
            return value
