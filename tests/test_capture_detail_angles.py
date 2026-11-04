"""Capture Detail compares a check-in with the employee's angles from SV HRM."""
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PyQt6.QtGui import QPixmap

from face_attendance import hrm_directory as hd
from face_attendance.hrm_sync import HRMError
from tests.test_dashboard import _app, temp_settings


def jpeg(value=120):
    ok, data = cv2.imencode(".jpg", np.full((40, 40, 3), value, dtype=np.uint8))
    assert ok
    return data.tobytes()


def listing(*keys):
    sha = hashlib.sha256(jpeg()).hexdigest()
    return {"angles": [{"angle": key, "sha256": sha, "version": 1} for key in keys]}


class CaptureDetailTests(unittest.TestCase):
    def setUp(self):
        from face_attendance.dashboard.app import MainWindow
        self.application = _app()
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.settings = temp_settings(self._root.name)
        self.settings.hrm_api_url = "http://127.0.0.1:9"
        self.settings.hrm_api_key = "k" * 40
        self.saved = ("up", "left")
        for target, function in (("list_angles", lambda _c, _code, opener=None: listing(*self.saved)),
                                 ("angle_image", lambda *a, **k: jpeg())):
            patcher = patch.object(hd.hrm_sync, target, side_effect=function)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Nothing here should reach out on its own during these tests.
        for target in ("configure",):
            patcher = patch("face_attendance.dashboard.hrm_live.HrmLiveLink." + target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = MainWindow(self.settings, use_lock=False)
        self.addCleanup(self.window.close)
        self.live = self.window.screens[0]
        self.addCleanup(lambda: self.live._activity_dialog and self.live._activity_dialog.reject())

    def _connect_to_hrm(self):
        hd._atomic_json(hd.cache_path(self.settings), {
            "version": hd.CACHE_VERSION, "fetched_at": 1.0, "source": "x",
            "employees": [{"employee_id": "E1", "employee_name": "Sok", "hrm_status": "ACTIVE"}]})

    def _open(self, code="E1"):
        self.live._open_activity_detail({"name": "Sok", "employee_id": code, "status": "Check-in saved",
                                         "capture": QPixmap()})
        return self.live._activity_dialog

    def _wait(self, condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.application.processEvents()
        return condition()

    def test_shows_the_hrm_angles_in_the_usual_order(self):
        self._connect_to_hrm()
        dialog = self._open()
        self.assertEqual(dialog.photo_count.text(), "Loading angles from SV HRM…")
        self.assertTrue(self._wait(lambda: dialog.photo_paths))
        self.assertEqual([path.stem for path in dialog.photo_paths], ["left", "up"])
        self.assertEqual(dialog.photo_count.text(), "1 / 2 enrolled angles  ·  Left")
        self.assertFalse(dialog.enrolled_photo._pixmap.isNull())
        dialog._navigate(1)
        self.assertEqual(dialog.photo_count.text(), "2 / 2 enrolled angles  ·  Up")

    def test_local_samples_are_not_shown_when_connected_to_hrm(self):
        self._connect_to_hrm()
        self.saved = ()
        with patch("face_attendance.dashboard.screens.live.enrollment_photos") as samples:
            dialog = self._open()
            self.assertTrue(self._wait(lambda: not dialog._angles_loading))
        samples.assert_not_called()
        self.assertEqual(dialog.photo_count.text(), "No enrolled angles in SV HRM")

    def test_with_hrm_unreachable_the_copies_here_still_show(self):
        self._connect_to_hrm()
        dialog = self._open()
        self._wait(lambda: dialog.photo_paths)
        dialog.reject()
        self.application.processEvents()
        with patch.object(hd.hrm_sync, "list_angles", side_effect=HRMError("HRM is down")):
            dialog = self._open()
            self.assertEqual(len(dialog.photo_paths), 2)
            self.assertTrue(self._wait(lambda: not dialog._angles_loading))
        self.assertEqual(len(dialog.photo_paths), 2)

    def test_stays_on_the_angle_being_viewed_when_the_list_updates(self):
        self._connect_to_hrm()
        dialog = self._open()
        self._wait(lambda: dialog.photo_paths)
        dialog._navigate(1)
        shown = dialog.photo_paths[dialog._photo_index]
        dialog.set_angle_photos([dialog.photo_paths[0].with_name("down.jpg"), *dialog.photo_paths])
        self.assertEqual(dialog.photo_paths[dialog._photo_index], shown)

    def test_a_late_answer_for_someone_else_is_ignored(self):
        self._connect_to_hrm()
        dialog = self._open()
        self._wait(lambda: not dialog._angles_loading)
        before = list(dialog.photo_paths)
        self.live._on_detail_angles("E2", True)
        self.assertEqual(dialog.photo_paths, before)

    def test_never_connected_to_hrm_keeps_the_local_photos(self):
        local = Path(self._root.name) / "sample.jpg"
        local.write_bytes(jpeg())
        with patch("face_attendance.dashboard.screens.live.enrollment_photos", return_value=[local]):
            dialog = self._open()
        self.assertEqual(dialog.photo_paths, [local])
        self.assertEqual(dialog.photo_count.text(), "1 / 1 enrolled images")
        hd.hrm_sync.list_angles.assert_not_called()


if __name__ == "__main__":
    unittest.main()
