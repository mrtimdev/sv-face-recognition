"""The Employee Profile window shows the angles stored in SV HRM."""
import hashlib
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PyQt6.QtWidgets import QLabel

from face_attendance import hrm_directory as hd
from face_attendance.hrm_sync import HRMError
from tests.test_dashboard import EmployeesScreenTests, _app


def jpeg(value=120):
    ok, data = cv2.imencode(".jpg", np.full((80, 80, 3), value, dtype=np.uint8))
    assert ok
    return data.tobytes()


def listing(*keys):
    sha = hashlib.sha256(jpeg()).hexdigest()
    return {"angles": [{"angle": key, "sha256": sha, "version": 1} for key in keys]}


class ProfileAngleTests(unittest.TestCase):
    def setUp(self):
        self.application = _app()
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.screen = EmployeesScreenTests()._screen(self._root.name)
        self.addCleanup(self.screen.deleteLater)
        self.screen.settings.hrm_api_url = "http://127.0.0.1:9"
        self.screen.settings.hrm_api_key = "k" * 40
        self.saved = {"E1": ("left", "up"), "E2": ("down",)}
        for target, function in (("list_angles", lambda _c, code, opener=None: listing(*self.saved[code])),
                                 ("angle_image", lambda *a, **k: jpeg())):
            patcher = patch.object(hd.hrm_sync, target, side_effect=function)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _row(self, code, in_hrm=True):
        return {"name": f"Label {code}", "employee_id": code, "employee_name": f"Name {code}", "samples": 1,
                "active": True, "in_hrm": in_hrm}

    def _settled(self):
        deadline = time.monotonic() + 5
        while self.screen._profile_angles_loading is not None and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.application.processEvents()
        return self.screen._profile_angles_loading is None

    def _tiles(self):
        tiles = []
        for index in range(self.screen.angles_layout.count()):
            widget = self.screen.angles_layout.itemAt(index).widget()
            if widget is not None:
                tiles.append(widget.findChildren(QLabel)[0])
        return tiles

    def _photos(self):
        return sum(1 for label in self._tiles() if label.pixmap() is not None and not label.pixmap().isNull())

    def test_shows_all_six_slots_with_the_saved_photos(self):
        self.screen._populate_profile(self._row("E1"))
        self.assertTrue(self._settled())
        self.assertEqual(len(self._tiles()), 6)
        self.assertEqual(self._photos(), 2)
        self.assertEqual(self.screen.angles_count_label.text(), "2 of 6 in SV HRM")

    def test_switching_employee_never_shows_the_previous_persons_angles(self):
        self.screen._populate_profile(self._row("E1"))
        self.screen._populate_profile(self._row("E2"))
        deadline = time.monotonic() + 5
        while self.screen.angles_count_label.text() != "1 of 6 in SV HRM" and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.assertEqual(self.screen.angles_count_label.text(), "1 of 6 in SV HRM")
        self.assertEqual(self._photos(), 1)

    def test_someone_not_in_hrm_is_explained_without_a_request(self):
        self.screen._populate_profile(self._row("L1", in_hrm=False))
        self.assertIsNone(self.screen._profile_angles_loading)
        self.assertIn("isn't in SV HRM", self.screen.angles_hint.text())
        hd.hrm_sync.list_angles.assert_not_called()

    def test_an_hrm_failure_is_shown(self):
        with patch.object(hd.hrm_sync, "list_angles", side_effect=HRMError("HRM is down")):
            self.screen._populate_profile(self._row("E1"))
            self.assertTrue(self._settled())
        self.assertIn("HRM is down", self.screen.angles_hint.text())
        self.assertEqual(self._photos(), 0)

    def test_a_live_change_refreshes_only_the_open_profile(self):
        row = self._row("E1")
        self.screen._populate_profile(row)
        self._settled()
        self.saved["E1"] = ("left", "up", "right")
        self.screen.on_hrm_angles_changed("E1")     # Window closed: nothing to do.
        self.assertIsNone(self.screen._profile_angles_loading)
        with patch.object(self.screen.editor, "isVisible", return_value=True), \
                patch.object(self.screen.right_stack, "currentIndex", return_value=1):
            self.screen.on_hrm_angles_changed("E2")
            self.assertIsNone(self.screen._profile_angles_loading)
            self.screen.on_hrm_angles_changed("e1")
        self.assertTrue(self._settled())
        self.assertEqual(self._photos(), 3)


if __name__ == "__main__":
    unittest.main()
