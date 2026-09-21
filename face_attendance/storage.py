"""Clean snapshot persistence; no UI or camera ownership."""
import logging
import os
import shutil
from pathlib import Path

import cv2

MIN_DISK_MB = 100


class SnapshotService:
    def __init__(self, directory):
        self.directory = Path(directory)

    def save(self, job):
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        try:
            free_mb = shutil.disk_usage(self.directory).free / (1024 * 1024)
            if free_mb < MIN_DISK_MB:
                logging.warning("Disk space low (%.0f MB free), skipping snapshot", free_mb)
                return ""
        except OSError:
            pass
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in job.employee_name)[:60]
        path = self.directory / f"{safe}_{job.event_id}.jpg"
        temporary = path.with_suffix(".part.jpg")
        try:
            if not cv2.imwrite(str(temporary), job.frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
                raise OSError("OpenCV could not write the evidence image")
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(path)

    @staticmethod
    def remove(path):
        if path:
            Path(path).unlink(missing_ok=True)
