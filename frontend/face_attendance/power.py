"""Prevent idle system sleep only while monitoring is active on macOS."""
import logging
import os
import subprocess
import sys


class SleepInhibitor:
    def __init__(self):
        self.process = None

    def acquire(self):
        if sys.platform != 'darwin' or self.process is not None:
            return
        try:
            # -w also releases the assertion if Python crashes or is killed.
            # The display can still sleep; no persistent power settings change.
            self.process = subprocess.Popen(
                ['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True)
        except OSError:
            logging.warning('Could not prevent idle sleep during monitoring', exc_info=True)

    def release(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        except OSError:
            logging.debug('Sleep assertion already released', exc_info=True)
