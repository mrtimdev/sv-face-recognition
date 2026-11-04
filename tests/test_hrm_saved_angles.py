"""AI Face Angles shows the angles HRM already stores for the chosen employee."""
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
from PyQt6.QtWidgets import QApplication

from face_attendance import hrm_directory as hd
from face_attendance.dashboard.screens.ai_angles import AIAnglesScreen, EXISTING
from face_attendance.hrm_sync import HRMConfig, HRMError
from face_attendance.settings import Settings

APP = QApplication.instance() or QApplication([])
CONFIG = HRMConfig("http://127.0.0.1:9", "k" * 40)


def jpeg(value=120):
    ok, data = cv2.imencode(".jpg", np.full((80, 80, 3), value, dtype=np.uint8))
    assert ok
    return data.tobytes()


def stored(*keys, data=None):
    data = data or jpeg()
    return {"angles": [{"angle": key, "sha256": hashlib.sha256(data).hexdigest(), "version": 2} for key in keys]}


def pump(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()
    return condition()


class Base(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.settings = Settings(encodings_path=str(self.root / "faces.pickle"),
                                 employees_path=str(self.root / "employees.json"),
                                 db_path=str(self.root / "a.db"), hrm_api_url=CONFIG.base_url,
                                 hrm_api_key=CONFIG.api_key)
        self.settings._path = self.root / "settings.json"


class FetchTests(Base):
    def _fetch(self, listing, images):
        with patch.object(hd.hrm_sync, "list_angles", return_value=listing), \
                patch.object(hd.hrm_sync, "angle_image", side_effect=images) as download:
            items, problems = hd.fetch_saved_angles(CONFIG, self.settings, "EMP015")
        return items, problems, download

    def test_downloads_once_then_reuses_the_unchanged_file(self):
        items, problems, download = self._fetch(stored("left"), [jpeg()])
        self.assertEqual(([item["key"] for item in items], problems, download.call_count), (["left"], [], 1))
        self.assertEqual(items[0]["path"].read_bytes(), jpeg())
        self.assertEqual(oct(items[0]["path"].stat().st_mode & 0o077), "0o0")
        _, _, download = self._fetch(stored("left"), [])
        download.assert_not_called()

    def test_a_changed_image_is_downloaded_again(self):
        self._fetch(stored("left"), [jpeg()])
        items, _, download = self._fetch(stored("left", data=jpeg(30)), [jpeg(30)])
        download.assert_called_once()
        self.assertEqual(items[0]["path"].read_bytes(), jpeg(30))

    def test_damaged_or_failed_images_are_reported_and_others_still_load(self):
        data = jpeg()
        listing = stored("straight", "left", "right")
        items, problems, _ = self._fetch(listing, [b"<html>", HRMError("HTTP 500"), data])
        self.assertEqual([item["key"] for item in items], ["right"])
        self.assertEqual(len(problems), 2)
        self.assertFalse((hd.saved_angles_dir(self.settings, "EMP015") / "straight.jpg").exists())

    def test_an_unknown_angle_name_never_becomes_a_file(self):
        items, problems, download = self._fetch({"angles": [{"angle": "../../evil", "sha256": "x"}]}, [])
        self.assertEqual((items, problems), ([], []))
        download.assert_not_called()

    def test_an_angle_deleted_in_hrm_is_removed_here(self):
        self._fetch(stored("left"), [jpeg()])
        self._fetch({"angles": []}, [])
        self.assertFalse((hd.saved_angles_dir(self.settings, "EMP015") / "left.jpg").exists())


class ScreenTests(Base):
    def setUp(self):
        super().setUp()
        hd._atomic_json(hd.cache_path(self.settings), {
            "version": hd.CACHE_VERSION, "fetched_at": 1.0, "source": "x", "employees": [
                {"employee_id": "EMP015", "employee_name": "Nget Tim", "hrm_status": "ACTIVE"},
                {"employee_id": "EMP020", "employee_name": "Sok Dara", "hrm_status": "ACTIVE"}]})
        self.listing = patch.object(hd.hrm_sync, "list_angles", side_effect=self._list)
        self.download = patch.object(hd.hrm_sync, "angle_image", side_effect=lambda *a, **k: jpeg())
        self.listing.start()
        self.download.start()
        self.addCleanup(self.listing.stop)
        self.addCleanup(self.download.stop)
        self.saved = {"EMP015": ("left", "up"), "EMP020": ("down",)}
        self.screen = AIAnglesScreen(None, self.settings)
        self.addCleanup(self.screen.deleteLater)
        self.screen.person_stack.setCurrentIndex(EXISTING)

    def _list(self, _config, code, opener=None):
        return stored(*self.saved[code])

    def _select(self, code):
        combo = self.screen.employee_combo
        combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i)["employee_id"] == code))
        self.screen._employee_changed()

    def _settled(self):
        return pump(lambda: self.screen._angles_loading is None)

    def test_choosing_an_employee_fills_their_saved_angles(self):
        self._select("EMP015")
        self.assertTrue(self._settled())
        tiles = self.screen.tiles
        self.assertTrue(tiles["left"].path and tiles["up"].path)
        self.assertEqual(tiles["left"].status.text(), "Saved in HRM · v2")
        self.assertIsNone(tiles["right"].path)
        self.assertEqual(tiles["right"].status.text(), "Waiting")

    def test_loaded_angles_are_not_preselected_for_enrollment_or_sent_back(self):
        self._select("EMP015")
        self._settled()
        self.assertEqual(self.screen._included_paths(), [])
        self.assertFalse(self.screen.enroll_button.isEnabled())
        with patch("face_attendance.dashboard.screens.ai_angles.HRMUploadWorker") as worker:
            self.screen._send_to_hrm()
        worker.assert_not_called()

    def test_ticking_use_allows_adding_a_loaded_angle(self):
        self._select("EMP015")
        self._settled()
        self.screen.tiles["left"].include.setChecked(True)
        self.assertEqual(self.screen._included_paths(), [self.screen.tiles["left"].path])

    def test_a_late_answer_for_the_previous_employee_is_ignored(self):
        self._select("EMP015")
        self._select("EMP020")
        self.assertTrue(self._settled())
        pump(lambda: False, 0.2)
        self.assertTrue(self.screen.tiles["down"].path)
        self.assertIsNone(self.screen.tiles["left"].path)

    def test_a_newly_generated_image_is_never_replaced(self):
        self._select("EMP015")
        self._settled()
        fresh = self.root / "fresh.jpg"
        fresh.write_bytes(jpeg(200))
        self.screen.tiles["left"].set_result(fresh)
        self.screen.on_hrm_angles_changed("emp015")
        self.assertTrue(self._settled())
        self.assertEqual(self.screen.tiles["left"].path, fresh)

    def test_a_live_change_updates_the_employee_on_screen_only(self):
        self._select("EMP015")
        self._settled()
        self.saved["EMP015"] = ("left", "up", "right")
        self.screen.on_hrm_angles_changed("EMP020")
        self.assertIsNone(self.screen._angles_loading)
        self.screen.on_hrm_angles_changed("EMP015")
        self.assertTrue(self._settled())
        self.assertTrue(self.screen.tiles["right"].path)

    def test_an_hrm_failure_says_so_and_leaves_the_tiles_usable(self):
        with patch.object(hd.hrm_sync, "list_angles", side_effect=HRMError("HRM is down")):
            self._select("EMP015")
            self.assertTrue(self._settled())
        self.assertEqual(self.screen.tiles["left"].status.text(), "Waiting")


if __name__ == "__main__":
    unittest.main()
