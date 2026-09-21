"""Non-blocking sound player for the dashboard.

Reuses the same ``simpleaudio`` backend as the persistence worker but adds a
per-event cooldown so rapid-fire captures (multiple faces in one frame) produce
a single pleasant notification rather than an overlapping cacophony.
"""
import logging
import time
import threading


class SoundPlayer:
    """Play a WAV file with cooldown; safe to call from any thread."""

    def __init__(self, path, cooldown_sec=1.0):
        self._path = path
        self._cooldown = cooldown_sec
        self._last_played = float("-inf")
        self._wave = None
        self._playback = None
        self._lock = threading.Lock()
        self._loaded = False
        self._load()

    def _load(self):
        from pathlib import Path
        path = Path(self._path)
        if not path.exists():
            logging.info("Sound file not found: %s", path)
            return
        try:
            import simpleaudio
            self._wave = simpleaudio.WaveObject.from_wave_file(str(path))
            self._loaded = True
        except Exception as exc:
            logging.info("Dashboard audio unavailable: %s", exc)

    @property
    def available(self):
        return self._loaded

    def play(self):
        """Play the sound if cooldown has elapsed; non-blocking."""
        if self._wave is None:
            return False
        now = time.monotonic()
        with self._lock:
            if now - self._last_played < self._cooldown:
                return False
            self._last_played = now
        try:
            self._playback = self._wave.play()
            return True
        except Exception:
            logging.debug("Dashboard sound playback failed", exc_info=True)
            return False

    def stop(self):
        if self._playback is not None:
            try:
                self._playback.stop()
            except Exception:
                pass
