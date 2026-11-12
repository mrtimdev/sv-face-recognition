"""AI Face Angles shows the angles HRM already stores for the chosen employee."""
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


class ScreenCase(Base):
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
        verify = patch.object(AIAnglesScreen, "_verify_saved")   # These plain images have no face to check.
        verify.start()
        self.addCleanup(verify.stop)
        self.saved ={"EMP015": ("left", "up"), "EMP020": ("down",)}
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


class ScreenTests(ScreenCase):
    def test_choosing_an_employee_fills_their_saved_angles(self):
        self._select("EMP015")
        self.assertTrue(self._settled())
        tiles = self.screen.tiles
        self.assertTrue(tiles["left"].path and tiles["up"].path)
        self.assertEqual(tiles["left"].status.text(), "Saved in HRM · v2")
        self.assertIsNone(tiles["right"].path)
        self.assertEqual(tiles["right"].status.text(), "Waiting")

    def test_loaded_angles_are_ticked_for_enrollment_but_not_sent_back(self):
        self._select("EMP015")
        self._settled()
        tiles = self.screen.tiles
        self.assertEqual(set(self.screen._included_paths()), {tiles["left"].path, tiles["up"].path})
        self.assertTrue(self.screen.enroll_button.isEnabled())
        with patch("face_attendance.dashboard.screens.ai_angles.HRMUploadWorker") as worker:
            self.screen._send_to_hrm()
        worker.assert_not_called()

    def test_unticking_use_survives_a_live_update_until_the_image_changes(self):
        self._select("EMP015")
        self._settled()
        tile = self.screen.tiles["left"]
        tile.include.click()
        self.assertFalse(tile.include.isChecked())
        self.screen.on_hrm_angles_changed("EMP015")
        self.assertTrue(self._settled())
        self.assertFalse(tile.include.isChecked())
        self.assertTrue(self.screen.tiles["up"].include.isChecked())
        self.listing.stop()
        with patch.object(hd.hrm_sync, "list_angles", return_value=stored("left", "up", data=jpeg(200))), \
                patch.object(hd.hrm_sync, "angle_image", side_effect=lambda *a, **k: jpeg(200)):
            self.screen.on_hrm_angles_changed("EMP015")
            self.assertTrue(self._settled())
        self.listing.start()
        self.assertTrue(tile.include.isChecked())

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


class RemoveTests(ScreenCase):
    YES = patch("face_attendance.dashboard.screens.ai_angles.QMessageBox.question",
                return_value=__import__("PyQt6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.StandardButton.Yes)

    def setUp(self):
        super().setUp()
        self.changed = []
        self.screen.anglesChanged.connect(self.changed.append)
        self._select("EMP015")
        self._settled()

    def _removed(self, key):
        return pump(lambda: (("EMP015", key) not in self.screen._removing))

    def test_remove_shows_only_on_filled_tiles(self):
        self.assertTrue(self.screen.tiles["left"].discard.isVisibleTo(self.screen))
        self.assertFalse(self.screen.tiles["right"].discard.isVisibleTo(self.screen))

    def test_removing_a_saved_angle_deletes_it_in_hrm_and_clears_the_tile(self):
        with self.YES, patch("face_attendance.dashboard.screens.ai_angles.delete_angle") as delete:
            self.screen.tiles["left"].discard.click()
            self.assertTrue(self._removed("left"))
        delete.assert_called_once()
        self.assertEqual(delete.call_args.args[1:], ("EMP015", "left"))
        self.assertIsNone(self.screen.tiles["left"].path)
        self.assertTrue(self.screen.tiles["up"].path)
        self.assertEqual(self.changed, ["EMP015"])

    def test_cancel_keeps_the_angle(self):
        no = patch("face_attendance.dashboard.screens.ai_angles.QMessageBox.question",
                   return_value=__import__("PyQt6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.StandardButton.No)
        with no, patch("face_attendance.dashboard.screens.ai_angles.delete_angle") as delete:
            self.screen.tiles["left"].discard.click()
        delete.assert_not_called()
        self.assertTrue(self.screen.tiles["left"].path)

    def test_a_failed_removal_keeps_the_tile(self):
        with self.YES, patch("face_attendance.dashboard.screens.ai_angles.delete_angle",
                             side_effect=HRMError("No permission", 403)):
            self.screen.tiles["left"].discard.click()
            self.assertTrue(self._removed("left"))
        self.assertTrue(self.screen.tiles["left"].path)
        self.assertEqual(self.screen.tiles["left"].status.text(), "Saved in HRM")
        self.assertEqual(self.changed, [])

    def test_switching_employee_during_removal_leaves_the_new_tiles_alone(self):
        def slow_delete(*_args):
            time.sleep(0.2)
        with self.YES, patch("face_attendance.dashboard.screens.ai_angles.delete_angle", side_effect=slow_delete):
            self.screen.tiles["left"].discard.click()
            self._select("EMP020")
            self.assertTrue(self._removed("left"))
            self.assertTrue(self._settled())
        self.assertTrue(self.screen.tiles["down"].path)
        self.assertEqual(self.changed, ["EMP015"])

    def test_an_unsaved_generated_image_is_cleared_without_hrm(self):
        fresh = self.root / "fresh.jpg"
        fresh.write_bytes(jpeg(200))
        tile = self.screen.tiles["right"]
        tile.set_result(fresh)
        self.screen._update_buttons()
        with patch("face_attendance.dashboard.screens.ai_angles.delete_angle") as delete:
            tile.discard.click()
        delete.assert_not_called()
        self.assertIsNone(tile.path)


class FakeChecker:
    def __init__(self, result, source_problem=""):
        self.result, self.problem = result, source_problem

    def source_problem(self):
        return self.problem

    def __call__(self, _content):
        return self.result


def checker(result, problem=""):
    return patch("face_attendance.dashboard.screens.ai_angles.angle_templates.generated_image_checker",
                 return_value=FakeChecker(result, problem))


class DroppedWorkerTests(Base):
    def _run(self, result, image_bytes=None, problem=""):
        from face_attendance.dashboard.screens.ai_angles import DroppedAngleWorker
        source = self.root / "drop.png"
        source.write_bytes(image_bytes if image_bytes is not None else jpeg(90))
        uploaded, events = [], []
        with checker(result, problem):
            worker = DroppedAngleWorker(b"src", "left", str(source), self.root / "out",
                                        lambda key, path: uploaded.append((key, Path(path))))
        for name in ("angleDone", "angleChecked", "angleFailed", "angleUploaded", "angleHeld", "runDone"):
            getattr(worker, name).connect(lambda *args, name=name: events.append((name, args)))
        worker.run()
        APP.processEvents()
        return uploaded, dict((name, args) for name, args in events)

    def test_a_matching_photo_is_saved_as_jpeg_and_sent_to_hrm(self):
        uploaded, events = self._run("matched")
        self.assertEqual(len(uploaded), 1)
        key, path = uploaded[0]
        self.assertEqual(key, "left")
        self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")
        self.assertEqual(events["angleUploaded"], ("left", ""))
        self.assertEqual(events["runDone"], (False,))

    def test_a_photo_of_someone_else_is_kept_but_not_sent(self):
        uploaded, events = self._run("mismatch")
        self.assertEqual(uploaded, [])
        self.assertIn("angleHeld", events)
        self.assertIn("angleDone", events)

    def test_an_unreadable_file_fails_without_sending(self):
        uploaded, events = self._run("matched", image_bytes=b"not an image")
        self.assertEqual(uploaded, [])
        self.assertEqual(events["angleFailed"][0], "left")
        self.assertNotIn("angleDone", events)


class DropScreenTests(ScreenCase):
    def setUp(self):
        super().setUp()
        self.changed = []
        self.screen.anglesChanged.connect(self.changed.append)
        self._select("EMP015")
        self._settled()
        self.photo = self.root / "new left.jpg"
        self.photo.write_bytes(jpeg(60))

    def test_tiles_only_take_image_files(self):
        from PyQt6.QtCore import QMimeData, QUrl
        tile = self.screen.tiles["left"]
        def event(name):
            data = QMimeData()
            data.setUrls([QUrl.fromLocalFile(str(self.root / name))])
            return SimpleNamespace(mimeData=lambda: data)
        self.assertTrue(tile.acceptDrops())
        self.assertEqual(tile._dropped_image(event("a.JPG")), str(self.root / "a.JPG"))
        self.assertEqual(tile._dropped_image(event("notes.pdf")), "")

    def test_without_a_profile_photo_a_drop_explains_why(self):
        self.screen._set_source(None)
        with patch.object(self.screen.toast, "show_message") as toast:
            self.screen._drop_angle("left", str(self.photo))
        self.assertIsNone(self.screen._worker)
        self.assertIn("profile photo", toast.call_args.args[0])

    def test_a_matching_drop_replaces_the_angle_in_hrm(self):
        self.screen._set_source(np.full((80, 80, 3), 120, dtype=np.uint8))
        sent = []
        with checker("matched"), patch.object(self.screen, "_hrm_uploader",
                                              return_value=(lambda key, path: sent.append(key), "")):
            self.screen._drop_angle("left", str(self.photo))
            self.assertTrue(pump(lambda: self.screen._worker is None))
        tile = self.screen.tiles["left"]
        self.assertEqual(sent, ["left"])
        self.assertTrue(tile.path and tile.hrm_sent)
        self.assertIn("dropped", Path(tile.path).name)
        self.assertEqual(self.changed, ["EMP015"])

    def test_a_drop_that_fails_shows_the_hrm_photo_again(self):
        self.screen._set_source(np.full((80, 80, 3), 120, dtype=np.uint8))
        bad = self.root / "broken.jpg"
        bad.write_bytes(b"x")
        from face_attendance.dashboard.screens import ai_angles
        real, seen = ai_angles.read_image, []

        def read(path):
            # Readable at drop time, broken by the time the worker copies it.
            if Path(path) != bad:
                return real(path)
            seen.append(path)
            return np.zeros((8, 8, 3), np.uint8) if len(seen) == 1 else None

        with patch.object(self.screen, "_hrm_uploader", return_value=(None, "")), checker("matched"):
            with patch("face_attendance.dashboard.screens.ai_angles.read_image", side_effect=read):
                self.screen._drop_angle("left", str(bad))
                self.assertTrue(pump(lambda: self.screen._worker is None))
            self.assertTrue(self._settled())
        self.assertTrue(self.screen.tiles["left"].path)
        self.assertTrue(self.screen.tiles["left"].from_hrm)


class AutoEnrollTests(ScreenCase):
    """A live update adds verified angles to enrollment without asking."""

    def setUp(self):
        super().setUp()
        self.checks = {"left": "matched", "up": "matched", "right": "mismatch", "down": None, "blink": "not_compared"}
        self.screen.engine = MagicMock()
        self.screen.runner = MagicMock()
        self._select("EMP015")
        self._settled()
        self.screen.source = np.zeros((80, 80, 3), dtype=np.uint8)

    def _list(self, _config, code, opener=None):
        listing = stored(*self.checks) if code == "EMP015" else stored("down")
        for row in listing["angles"]:
            row["verificationStatus"] = self.checks.get(row["angle"])
        return listing

    def _live_update(self):
        self.screen.on_hrm_angles_changed("EMP015")
        self.assertTrue(self._settled())

    def _submitted(self):
        return self.screen.runner.submit.call_args

    def _finish(self, status="enrolled"):
        if status == "enrolled":   # They now have a local enrollment.
            self.screen.service = MagicMock()
            self.screen.service.employees.return_value = [{"name": "Nget Tim", "employee_id": "EMP015",
                                                           "employee_name": "Nget Tim", "samples": 3, "active": True}]
        self.screen._on_enrolled(SimpleNamespace(status=status, ok=status == "enrolled", message="done", total_samples=3))
        self.screen.runner.submit.reset_mock()
        self.assertTrue(self._settled())

    def test_only_a_live_update_adds_and_only_verified_ticked_images(self):
        self.screen.runner.submit.assert_not_called()   # Choosing the employee doesn't add anything.
        self._live_update()
        images, name, code = self._submitted().args[:3]
        self.assertEqual(code, "EMP015")
        self.assertEqual(len(images), 3)                # The profile photo plus left and up.
        self.assertIs(images[0], self.screen.source)
        self.assertEqual(set(self.screen._enroll_added[1]), {"left", "up"})

    def test_the_same_image_is_added_once_even_after_a_restart(self):
        self._live_update()
        self._finish()
        self._live_update()
        self.screen.runner.submit.assert_not_called()
        self.assertEqual(set(hd.auto_enrolled(self.settings, "emp015")), {"left", "up"})

    def test_a_newly_verified_angle_is_added_by_itself(self):
        self._live_update()
        self._finish()
        self.checks["down"] = "matched"
        self._live_update()
        images = self._submitted().args[0]
        self.assertEqual(len(images), 1)
        self.assertEqual(set(self.screen._enroll_added[1]), {"down"})

    def test_a_failed_save_is_tried_again_on_the_next_update(self):
        self._live_update()
        self._finish("error")
        self._live_update()
        self.screen.runner.submit.assert_called_once()

    def test_an_unticked_image_is_left_out(self):
        self.screen.tiles["left"].include.click()
        self._live_update()
        self.assertEqual(set(self.screen._enroll_added[1]), {"up"})

    def test_a_first_enrollment_waits_for_the_profile_photo(self):
        self.screen.source = None
        self._live_update()
        self.screen.runner.submit.assert_not_called()
        self.assertTrue(self.screen._auto_enroll)
        self.screen.source = np.zeros((80, 80, 3), dtype=np.uint8)
        self.screen._auto_add_to_enrollment()
        self.screen.runner.submit.assert_called_once()

    def test_an_account_without_permission_adds_nothing(self):
        access = MagicMock()
        access.allows.return_value = False
        with patch("face_attendance.dashboard.screens.ai_angles.access_for", return_value=access):
            self._live_update()
        self.screen.runner.submit.assert_not_called()
        access.deny.assert_not_called()   # Nobody asked for this, so there's nothing to refuse.

    def test_an_update_during_a_run_is_handled_when_the_run_ends(self):
        self.screen._saving = True
        self.screen.on_hrm_angles_changed("EMP015")
        self.assertIsNone(self.screen._angles_loading)
        self.screen.runner.submit.assert_not_called()
        self.screen._saving = False
        self.screen._update_buttons()
        self.assertTrue(self._settled())
        self.screen.runner.submit.assert_called_once()

    def test_an_image_this_app_sent_to_hrm_becomes_the_saved_copy_and_is_added(self):
        fresh = self.root / "fresh.jpg"
        fresh.write_bytes(jpeg(200))
        tile = self.screen.tiles["left"]
        tile.set_result(fresh, "matched")
        tile.set_hrm_result(None)
        self._live_update()
        self.assertTrue(tile.from_hrm)
        self.assertIn("left", self.screen._enroll_added[1])


if __name__ == "__main__":
    unittest.main()
