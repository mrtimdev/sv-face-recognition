"""Clean snapshot persistence; no UI or camera ownership."""
import logging
import json
import os
import shutil
from pathlib import Path

import cv2
import numpy as np

MIN_DISK_MB = 100


def read_image(path):
    """Decode an image file (BGR), or None when it can't be read.

    Unlike ``cv2.imread``, this opens any path on Windows, including folders
    with non-ASCII names.  EXIF orientation is applied the same way.
    """
    try:
        data = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
    except OSError:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def write_jpeg(path, frame, quality=92):
    """Write *frame* as a JPEG and flush it to disk through one writable handle.

    Windows can only fsync a handle opened for writing (a read-only one fails
    with "[Errno 9] Bad file descriptor"), and ``cv2.imwrite`` can't open
    non-ASCII paths there, so the image is encoded in memory first.
    """
    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise OSError("OpenCV could not encode the image")
    with open(path, "wb") as handle:
        handle.write(encoded.tobytes())
        handle.flush()
        os.fsync(handle.fileno())


def context_path(snapshot):
    """Full-frame companion; kept out of the normal portrait gallery."""
    path = Path(snapshot)
    return path.parent / "context" / path.name


def evidence_files(snapshot):
    if not snapshot:
        return ()
    context = context_path(snapshot)
    return Path(snapshot), context, context.with_suffix(".json")


def delete_capture(snapshot, directory):
    """Delete one gallery capture and its companions, preserving attendance rows.

    Keep the thumbnail until companion deletion succeeds so a filesystem error
    leaves the capture visible for retry. Never traverse outside the capture root.
    """
    root = Path(directory).resolve()
    path = Path(snapshot)
    if path.parent.resolve() != root or path.suffix.lower() != ".jpg":
        raise ValueError("Select an evidence image in the configured capture folder")
    artifacts = evidence_files(path)
    for artifact in artifacts:
        expected = root if artifact == path else root / "context"
        if artifact.is_symlink() or artifact.resolve().parent != expected:
            raise ValueError("Evidence links outside the capture folder cannot be deleted here")
    removed = 0
    for artifact in (*artifacts[1:], artifacts[0]):
        try:
            artifact.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    return removed


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
        created = []
        try:
            if job.context_frame is not None:
                context = context_path(path)
                context.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._write_image(context, job.context_frame)
                created.append(context)
                metadata = {"version": 1, "event_id": job.event_id,
                            "employee_id": job.employee_id, "track_id": job.track_id,
                            "captured_at": job.captured_at,
                            "source_sequence": job.source_sequence,
                            "source_generation": job.source_generation,
                            "box_order": "top,right,bottom,left",
                            "face_box": list(map(float, job.face_box)) if job.face_box else None,
                            "crop_box": list(job.crop_box) if job.crop_box else None,
                            "source_size": [job.context_frame.shape[1], job.context_frame.shape[0]]}
                metadata_path = context.with_suffix(".json")
                temporary = metadata_path.with_suffix(".part.json")
                try:
                    with temporary.open("w", encoding="utf-8") as handle:
                        json.dump(metadata, handle, ensure_ascii=False, allow_nan=False)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, metadata_path)
                finally:
                    temporary.unlink(missing_ok=True)
                created.append(metadata_path)
            self._write_image(path, job.frame)
            return str(path)
        except Exception:
            for artifact in created:
                artifact.unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_image(path, frame):
        temporary = path.with_suffix(".part.jpg")
        try:
            write_jpeg(temporary, frame)
            os.replace(temporary, path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def remove(path):
        removed_images = 0
        for artifact in evidence_files(path):
            if artifact.is_file():
                artifact.unlink()
                removed_images += artifact.suffix.lower() == ".jpg"
        return removed_images
