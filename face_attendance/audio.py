"""Nonblocking WAV playback shared by dashboard and terminal workers."""
import logging
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time


class SoundPlayer:
    def __init__(self, path, cooldown_sec=1.0):
        self._path = str(Path(path).expanduser().resolve())
        self._cooldown = cooldown_sec
        self._last_played = float("-inf")
        self._lock = threading.Lock()
        self._wave = self._playback = self._process = None
        self._command = self._winsound = None
        if not Path(self._path).is_file():
            logging.debug("Sound file not found: %s", self._path)
            return
        # Native backends need no compiled third-party Python extension.
        if sys.platform == "darwin" and Path("/usr/bin/afplay").is_file():
            self._command = ["/usr/bin/afplay", self._path]
        elif sys.platform == "win32":
            import winsound
            self._winsound = winsound
        else:
            try:
                import simpleaudio
                self._wave = simpleaudio.WaveObject.from_wave_file(self._path)
            except Exception:
                player = shutil.which("paplay") or shutil.which("aplay")
                if player:
                    self._command = [player, self._path]
                else:
                    logging.info("Audio unavailable: install simpleaudio or a system WAV player")

    @property
    def available(self):
        return any(value is not None for value in (self._wave, self._command, self._winsound))

    def play(self):
        with self._lock:
            now = time.monotonic()
            if not self.available or now - self._last_played < self._cooldown:
                return False
            if self._process is not None and self._process.poll() is None:
                return False
            try:
                if self._command:
                    self._process = subprocess.Popen(
                        self._command, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                elif self._winsound:
                    self._winsound.PlaySound(self._path, self._winsound.SND_FILENAME |
                                             self._winsound.SND_ASYNC | self._winsound.SND_NODEFAULT)
                else:
                    if self._playback is not None and self._playback.is_playing():
                        return False
                    self._playback = self._wave.play()
            except Exception:
                logging.warning("Sound playback failed for %s", self._path, exc_info=True)
                return False
            self._last_played = now
            return True

    def stop(self):
        with self._lock:
            try:
                if self._process is not None:
                    if self._process.poll() is None:
                        self._process.terminate()
                    try:
                        self._process.wait(timeout=.2)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=.2)
                    self._process = None
                if self._playback is not None:
                    self._playback.stop()
                    self._playback = None
                if self._winsound:
                    self._winsound.PlaySound(None, 0)
            except Exception:
                logging.debug("Sound cleanup failed", exc_info=True)
