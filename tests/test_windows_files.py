"""Windows file behaviour the app must survive, simulated on any OS.

On Windows ``os.fsync`` only works on a handle opened for writing; a read-only
handle fails with "[Errno 9] Bad file descriptor".  Re-opening a saved JPEG
read-only to fsync it broke enrollment photos and attendance snapshots in the
Windows build.  ``cv2.imread``/``imwrite`` also can't open non-ASCII paths
there, so images are decoded and encoded in memory.
"""
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

try:
    import fcntl
except ImportError:          # on Windows the real os.fsync already behaves this way
    fcntl = None

from face_attendance.enrollment import EnrollmentService
from face_attendance.enrollment_photos import sample_photos
from face_attendance.storage import SnapshotService, read_image, write_jpeg
from tests.test_enrollment_photos import PhotoBackend, frame

_real_fsync = os.fsync


def windows_fsync(descriptor):
    if fcntl is not None and fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
        raise OSError(errno.EBADF, "Bad file descriptor")
    return _real_fsync(descriptor)


class WindowsFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = patch("os.fsync", side_effect=windows_fsync)
        patcher.start()
        self.addCleanup(patcher.stop)

    @unittest.skipIf(fcntl is None, "uses the real Windows behaviour instead")
    def test_the_simulation_rejects_read_only_handles_like_windows(self):
        path = self.root / "x.jpg"
        path.write_bytes(b"x")
        with open(path, "rb") as handle, self.assertRaises(OSError) as caught:
            os.fsync(handle.fileno())
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_enrolling_saves_photos(self):
        service = EnrollmentService(self.root / "faces.pickle", self.root / "employees.json",
                                    backend=PhotoBackend(), check_quality=False)
        outcome = service.enroll_many([frame(60), frame(140)], "Dara", "EMP-9")
        self.assertTrue(outcome.ok, outcome.message)
        photos = sample_photos(self.root / "faces.pickle", "Dara")
        self.assertEqual([int(read_image(path)[0, 0, 0]) for path in photos], [60, 140])

    def test_attendance_snapshots_save(self):
        captures = self.root / "captures"
        captures.mkdir()
        target = captures / "Dara_event.jpg"
        SnapshotService._write_image(target, frame(90))
        self.assertEqual(int(read_image(target)[0, 0, 0]), 90)
        self.assertEqual([p.name for p in captures.iterdir()], ["Dara_event.jpg"])

    def test_non_ascii_folders_and_unreadable_files(self):
        folder = self.root / "ទិន្នន័យ"      # e.g. a Khmer folder or Windows user name
        folder.mkdir()
        path = folder / "រូបថត.jpg"
        write_jpeg(path, np.full((40, 30, 3), 200, np.uint8))
        self.assertEqual(read_image(path).shape, (40, 30, 3))
        self.assertIsNone(read_image(folder / "missing.jpg"))
        (folder / "empty.jpg").write_bytes(b"")
        self.assertIsNone(read_image(folder / "empty.jpg"))
        (folder / "text.jpg").write_bytes(b"not an image")
        self.assertIsNone(read_image(folder / "text.jpg"))


if __name__ == "__main__":
    unittest.main()
