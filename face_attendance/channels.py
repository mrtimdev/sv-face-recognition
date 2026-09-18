"""A one-slot mailbox: slow consumers never build up a video backlog."""
import threading


class LatestValue:
    def __init__(self):
        self._value = None
        self._lock = threading.Lock()
        self.ready = threading.Event()

    def put(self, value):
        with self._lock:
            self._value = value
            self.ready.set()

    def get(self):
        with self._lock:
            return self._value

    def take(self):
        with self._lock:
            value, self._value = self._value, None
            self.ready.clear()
            return value
