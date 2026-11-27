"""Several saved cameras: settings migration, the Live Monitor grid, and an
engine running 2-4 cameras at once without one slow stream stalling the rest."""
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
import unittest.mock
from unittest.mock import Mock

import numpy as np

from tests.test_dashboard import _app, temp_settings
from tests.test_pipeline import FakeBackend
from face_attendance.settings import MAX_LIVE_CAMERAS, Settings, describe_source


SECRET_URL = "rtsp://admin:s3cret@192.0.2.10:554/Streaming/Channels/101"


class SettingsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "settings.json"

    def test_old_file_loads_as_one_camera_list(self):
        self.path.write_text(json.dumps({"source": SECRET_URL, "camera_width": 960}), encoding="utf-8")
        settings = Settings.load(self.path)
        self.assertEqual(settings.camera_entries(), [{"name": "Camera 1", "source": SECRET_URL}])
        self.assertEqual(settings.selected_cameras(), [(0, {"name": "Camera 1", "source": SECRET_URL})])
        self.assertEqual(settings.to_config().source, SECRET_URL)
        # Saving writes the migrated list without losing the old source.
        settings.save()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["source"], SECRET_URL)
        self.assertEqual(data["cameras"], [{"name": "Camera 1", "source": SECRET_URL}])
        self.assertEqual(data["live_cameras"], [0])

    def test_several_cameras_save_and_reload(self):
        settings = Settings()
        settings._path = self.path
        settings.update({"source": "0",
                         "cameras": [{"name": "Desk", "source": "0"},
                                     {"name": "Door", "source": SECRET_URL},
                                     {"name": "Lobby", "source": "rtsp://192.0.2.11/live"}],
                         "live_cameras": [0, 2]})
        settings.save()
        loaded = Settings.load(self.path)
        self.assertEqual([entry["name"] for entry in loaded.camera_entries()], ["Desk", "Door", "Lobby"])
        self.assertEqual([index for index, _ in loaded.selected_cameras()], [0, 2])
        self.assertEqual(loaded.camera_entries()[1]["source"], SECRET_URL)

    def test_source_stays_authoritative_for_the_first_camera(self):
        settings = Settings(cameras=[{"name": "Desk", "source": "0"}, {"name": "Door", "source": "1"}])
        changed = replace(settings, source="2")  # e.g. the first-run wizard or web settings
        self.assertEqual(changed.camera_entries()[0], {"name": "Desk", "source": "2"})
        self.assertEqual(changed.camera_entries()[1]["source"], "1")

    def test_selection_ignores_bad_indices_and_caps_at_four(self):
        cameras = [{"name": f"Cam {n}", "source": str(n)} for n in range(6)]
        settings = Settings(cameras=cameras, live_cameras=[9, 1, 1, 0, 2, 3, 4, 5])
        self.assertEqual([index for index, _ in settings.selected_cameras()], [1, 0, 2, 3])
        self.assertEqual(len(settings.selected_cameras()), MAX_LIVE_CAMERAS)
        self.assertEqual(Settings(live_cameras=[]).selected_cameras()[0][0], 0)

    def test_bad_camera_values_are_rejected(self):
        settings = Settings()
        with self.assertRaises(ValueError):
            settings.update({"cameras": "not a list"})
        with self.assertRaises(ValueError):
            settings.update({"live_cameras": ["front"]})
        settings.update({"cameras": [{"name": "Desk", "source": "0"}, {"name": "Door", "source": ""}]})
        with self.assertRaisesRegex(ValueError, "Door has no source"):
            settings.to_config()

    def test_camera_labels_mask_passwords(self):
        self.assertNotIn("s3cret", describe_source(SECRET_URL))


class RecognitionLaneTests(unittest.TestCase):
    def test_one_worker_serves_every_camera_in_turn(self):
        from face_attendance.models import FramePacket, RecognitionRequest
        from face_attendance.recognition import RecognitionResult, RecognitionWorker
        handled = []

        class Service:
            config = Settings().to_config()

            def process(self, request):
                handled.append(request.packet.sequence)
                time.sleep(0.01)
                return RecognitionResult(request.packet, (), 0.01)

        worker = RecognitionWorker(Service())
        lanes = [worker.lanes[0], worker.add_lane(), worker.add_lane()]
        worker.start()
        try:
            for round_ in range(3):
                for number, lane in enumerate(lanes):
                    packet = FramePacket(number * 100 + round_, time.monotonic(), time.time(), 1,
                                         np.zeros((8, 8, 3), np.uint8))
                    worker.submit(lane, RecognitionRequest(packet, ()))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not all(lane[1].get() is not None and
                                                              lane[1].get().packet.sequence % 100 == round_
                                                              for lane in lanes):
                    time.sleep(0.005)
                for number, lane in enumerate(lanes):
                    self.assertEqual(lane[1].get().packet.sequence, number * 100 + round_)
        finally:
            worker.close()
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(sorted({sequence // 100 for sequence in handled}), [0, 1, 2])


class FastCapture:
    def __init__(self, shape=(240, 320, 3)):
        self.shape = shape

    def isOpened(self):
        return True

    def read(self):
        threading.Event().wait(1 / 60)
        return True, np.zeros(self.shape, np.uint8)

    def release(self):
        pass


class HangingCapture(FastCapture):
    """An RTSP stream that opened, then stops answering until its read times out."""

    def __init__(self, release):
        super().__init__()
        self._release = release

    def read(self):
        self._release.wait(1.0)
        return False, None


class DeadCapture(FastCapture):
    def isOpened(self):
        return False


def multi_settings(directory, count):
    settings = temp_settings(directory)
    cameras = [{"name": f"Camera {n + 1}", "source": f"rtsp://192.0.2.{n + 1}/live"} for n in range(count)]
    settings = replace(settings, source=cameras[0]["source"], cameras=cameras,
                       live_cameras=list(range(count)), reconnect_sec=0.2)
    settings._path = Path(directory) / "settings.json"
    return settings


class MultiCameraEngineTests(unittest.TestCase):
    def run_engine(self, settings, factory, until, seconds=8):
        from face_attendance.dashboard.engine import AttendanceEngine
        application = _app()
        engine = AttendanceEngine(settings, backend=FakeBackend(), capture_factory=factory, use_lock=False)
        seen = {"primary": 0, "tiles": {}, "stats": None, "gaps": []}
        engine.frameReady.connect(lambda _frame: seen.__setitem__("primary", seen["primary"] + 1))
        engine.channelFrameReady.connect(
            lambda position, _frame: seen["tiles"].__setitem__(position, seen["tiles"].get(position, 0) + 1))
        engine.statsReady.connect(lambda stats: seen.__setitem__("stats", stats))
        try:
            engine.start()
            deadline = time.monotonic() + seconds
            last = time.monotonic()
            while time.monotonic() < deadline and not until(seen):
                application.processEvents()
                now = time.monotonic()
                seen["gaps"].append(now - last)
                last = now
                time.sleep(0.01)
        finally:
            started = time.monotonic()
            engine.shutdown()
            seen["stop_seconds"] = time.monotonic() - started
            application.processEvents()
        return engine, seen

    def test_four_cameras_start_publish_tiles_and_stop_every_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = multi_settings(directory, 4)

            def until(seen):
                stats = seen["stats"] or {}
                return (seen["primary"] >= 5 and all(seen["tiles"].get(p, 0) >= 5 for p in (1, 2, 3))
                        and [camera["status"] for camera in stats.get("cameras", ())] == ["CONNECTED"] * 4)

            engine, seen = self.run_engine(settings, FastCapture, until)
            self.assertTrue(until(seen), seen["tiles"])
            self.assertEqual(len(engine.channels), 4)
            self.assertEqual([camera["name"] for camera in seen["stats"]["cameras"]],
                             ["Camera 1", "Camera 2", "Camera 3", "Camera 4"])
            # Every camera has its own reader thread; all of them are joined.
            self.assertEqual(len({id(channel.camera.thread) for channel in engine.channels}), 4)
            for channel in engine.channels:
                self.assertFalse(channel.camera.thread.is_alive())
            self.assertFalse(engine.recognition.thread.is_alive())
            self.assertFalse(engine.persistence.thread.is_alive())
            # Face ids never collide between cameras.
            starts = [channel.tracker.next_id for channel in engine.channels]
            self.assertEqual(len(set(starts)), 4)

    def test_hung_and_dead_streams_do_not_stall_the_others_or_the_ui(self):
        release = threading.Event()
        made = []

        def factory():
            # Readers open in parallel, so pick by thread name, not by call order.
            name = threading.current_thread().name
            made.append(name)
            if name == "camera-2":
                return HangingCapture(release)
            if name == "camera-3":
                return DeadCapture()
            return FastCapture()

        with tempfile.TemporaryDirectory() as directory:
            settings = multi_settings(directory, 3)

            def until(seen):
                return seen["primary"] >= 30 and seen["stats"] is not None

            try:
                engine, seen = self.run_engine(settings, factory, until)
            finally:
                release.set()
            self.assertGreaterEqual(seen["primary"], 30, "the healthy camera stalled")
            statuses = {camera["name"]: camera["status"] for camera in seen["stats"]["cameras"]}
            self.assertEqual(statuses["Camera 1"], "CONNECTED")
            self.assertNotEqual(statuses["Camera 3"], "CONNECTED")
            # The overall status reports the live camera; recognition keeps running.
            self.assertEqual(seen["stats"]["status"], "CONNECTED")
            # The Qt event loop kept turning: no long freezes while the others hung.
            self.assertLess(max(seen["gaps"][1:] or [0]), 0.5)
            self.assertLess(seen["stop_seconds"], 6)

    def test_one_camera_pauses_reconnects_and_snapshots_on_its_own(self):
        from face_attendance.dashboard.engine import AttendanceEngine
        application = _app()
        opened = []

        def factory():
            opened.append(threading.current_thread().name)
            return FastCapture()

        with tempfile.TemporaryDirectory() as directory:
            settings = multi_settings(directory, 2)
            engine = AttendanceEngine(settings, backend=FakeBackend(), capture_factory=factory, use_lock=False)
            try:
                engine.start()

                def wait(condition, seconds=5):
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline and not condition():
                        application.processEvents()
                        time.sleep(0.01)
                    return condition()

                self.assertTrue(wait(lambda: engine.grab_clean_frame(1) is not None))
                self.assertEqual(engine.grab_clean_frame(1).shape, (240, 320, 3))
                engine.set_camera_paused(1, True)
                submitted = engine.channels[1].submitted
                time.sleep(0.4)
                self.assertEqual(engine.channels[1].submitted, submitted, "a paused camera kept recognizing")
                self.assertTrue(wait(lambda: engine.stats.get("cameras", [{}, {}])[1].get("paused")))
                engine.set_camera_paused(1, False)
                self.assertTrue(wait(lambda: engine.channels[1].submitted != submitted))

                old = engine.channels[1].camera
                self.assertTrue(engine.reconnect_camera(1))
                self.assertTrue(wait(lambda: engine.channels[1].camera is not old))
                self.assertFalse(old.thread.is_alive())
                self.assertIs(engine.channels[0].camera, engine.camera)  # The other camera is untouched.
                self.assertEqual(opened.count("camera-2"), 2)
                self.assertEqual(opened.count("camera"), 1)
                self.assertTrue(wait(lambda: engine.channels[1].status == "CONNECTED"))
            finally:
                engine.shutdown()
                application.processEvents()
            for channel in engine.channels:
                self.assertFalse(channel.camera.thread.is_alive())

    def test_single_camera_keeps_the_original_names(self):
        from face_attendance.dashboard.engine import AttendanceEngine
        _app()
        with tempfile.TemporaryDirectory() as directory:
            engine = AttendanceEngine(temp_settings(directory), backend=FakeBackend(), use_lock=False)
            self.assertEqual(len(engine.channels), 1)
            self.assertIs(engine.camera, engine.channels[0].camera)
            self.assertEqual(engine.camera.thread.name, "camera")
            self.assertIsNone(engine.camera.frame_event)
            self.assertEqual(engine.tracker.next_id, 1)


class LiveScreenCameraTests(unittest.TestCase):
    def setUp(self):
        from face_attendance.dashboard.engine import AttendanceEngine
        from face_attendance.dashboard.screens.live import LiveScreen
        self.app = _app()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "settings.json"
        settings = replace(multi_settings(self.directory.name, 3), live_cameras=[0])
        settings._path = self.path
        self.engine = AttendanceEngine(settings, backend=FakeBackend(), use_lock=False)
        self.screen = LiveScreen(self.engine, settings)
        from PyQt6 import sip
        self.addCleanup(lambda: sip.isdeleted(self.screen) or self.screen.deleteLater())

    def saved(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_chips_show_hide_cameras_and_build_one_tile_each(self):
        screen = self.screen
        bar = screen.camera_bar
        self.assertEqual([chip.text() for chip in bar.chips], ["Camera 1", "Camera 2", "Camera 3"])
        self.assertEqual([chip.isChecked() for chip in bar.chips], [True, False, False])
        applied = []
        screen.settingsApplied.connect(lambda settings, restart: applied.append(restart))
        bar.chips[2].click()
        bar.chips[1].click()
        self.assertEqual(len(screen.extra_videos), 2)
        self.assertEqual(screen.settings.live_cameras, [0, 2, 1])  # In the order they were shown.
        self.assertEqual([chip.isChecked() for chip in bar.chips], [True, True, True])
        self.assertIs(self.engine.settings, screen.settings)
        self.assertEqual(self.saved()["live_cameras"], [0, 2, 1])
        self.assertEqual(applied[-1], False)  # Not running: nothing restarted.
        frame = np.zeros((48, 64, 3), np.uint8)
        screen._on_channel_frame(1, frame)
        self.assertTrue(screen.extra_videos[0].has_frame())
        self.assertFalse(screen.extra_videos[1].has_frame())
        self.assertIn("Camera 3", screen.extra_videos[0].corner.text())
        # The last camera stays on.
        for chip in (bar.chips[1], bar.chips[2], bar.chips[0]):
            chip.click()
        self.assertEqual(screen.settings.live_cameras, [0])
        self.assertTrue(bar.chips[0].isChecked())
        self.assertEqual(screen.extra_videos, [])

    def test_tile_switcher_swaps_cameras_between_tiles(self):
        screen = self.screen
        screen._apply_cameras([0, 1])
        screen._switch_tile(0, 2)
        self.assertEqual(screen.settings.live_cameras, [2, 1])
        screen._switch_tile(1, 2)  # Camera 3 is on tile 1 already: the two swap.
        self.assertEqual(screen.settings.live_cameras, [1, 2])
        menu = screen.video.corner.menu()
        names = [action.text() for action in menu.actions() if action.isCheckable()]
        self.assertEqual(names, ["Camera 1", "Camera 2", "Camera 3"])

    def show(self):
        from PyQt6.QtWidgets import QMainWindow
        window = QMainWindow()
        window.setCentralWidget(self.screen)
        window.resize(1400, 1000)
        window.show()
        self.addCleanup(window.close)
        self.settle()
        return window

    def settle(self, seconds=0.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)

    def test_enlarge_animates_one_tile_to_full_size_and_back(self):
        screen = self.screen
        screen._apply_cameras([0, 1, 2])
        self.show()
        top, bottom = screen.tile_row_splitters
        self.assertEqual(top.count(), 2)
        self.assertEqual(bottom.count(), 1)
        screen._toggle_focus(1)
        self.assertTrue(screen._row_animator.running or screen._column_animators[0].running)
        self.settle()
        self.assertEqual(screen.tile_rows.sizes()[1], 0)
        self.assertEqual(top.sizes()[0], 0)
        self.assertGreater(top.sizes()[1], 0)
        self.assertEqual(screen.extra_videos[0].footer.enlarge.toolTip(), "Back to the grid")
        screen._toggle_focus(1)
        self.settle()
        widths = top.sizes()
        self.assertLessEqual(abs(widths[0] - widths[1]), 2)
        self.assertGreater(screen.tile_rows.sizes()[1], 0)

    def test_several_cameras_get_their_own_controls_instead_of_the_big_hud(self):
        screen = self.screen
        self.assertFalse(screen.preview_hud.isHidden())
        self.assertTrue(screen.engine_strip.isHidden())
        screen._apply_cameras([0, 1])
        self.assertTrue(screen.preview_hud.isHidden())
        self.assertFalse(screen.engine_strip.isHidden())
        controls = [view.footer for view in screen._tile_views()]
        self.assertTrue(all(control is not None and not control.isHidden() for control in controls))
        self.assertFalse(controls[0].pause.isEnabled())  # Engine stopped.
        screen._apply_cameras([0])
        self.assertFalse(screen.preview_hud.isHidden())
        self.assertTrue(screen.video.footer.isHidden())

    def test_tile_pause_is_per_camera(self):
        screen = self.screen
        screen._apply_cameras([0, 2])
        paused = []
        self.engine.cameraPausedChanged.connect(lambda index, value: paused.append((index, value)))
        screen.extra_videos[0].footer.pauseClicked.emit()
        self.assertEqual(paused, [(2, True)])
        self.assertTrue(self.engine.camera_paused(2))
        self.assertFalse(self.engine.camera_paused(0))
        self.assertIn("Paused", screen.extra_videos[0].corner.text())
        self.assertNotIn("Paused", screen.video.corner.text())
        screen.extra_videos[0].footer.pauseClicked.emit()
        self.assertFalse(self.engine.camera_paused(2))

    def test_add_edit_and_remove_from_the_monitor(self):
        from unittest.mock import patch
        from PyQt6.QtWidgets import QDialog, QMessageBox
        from face_attendance.dashboard.widgets import camera_bar
        screen = self.screen
        with patch.object(camera_bar.CameraDialog, "exec", return_value=QDialog.DialogCode.Accepted), \
                patch.object(camera_bar.CameraDialog, "values",
                             return_value={"name": "Gate", "source": SECRET_URL}):
            screen._edit_camera(None)
        self.assertEqual([chip.text() for chip in screen.camera_bar.chips][-1], "Gate")
        self.assertEqual(screen.settings.live_cameras, [0, 3])  # A new camera shows right away.
        self.assertEqual(self.saved()["cameras"][3]["source"], SECRET_URL)
        self.assertNotIn("s3cret", screen.camera_bar.chips[3].toolTip())
        self.assertNotIn("s3cret", screen.extra_videos[0].corner.toolTip())
        with patch.object(camera_bar.CameraDialog, "exec", return_value=QDialog.DialogCode.Accepted), \
                patch.object(camera_bar.CameraDialog, "values",
                             return_value={"name": "Main door", "source": "rtsp://192.0.2.99/live"}):
            screen._edit_camera(0)
        self.assertEqual(screen.settings.source, "rtsp://192.0.2.99/live")
        self.assertEqual(screen.camera_bar.chips[0].text(), "Main door")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            screen._remove_camera(0)
        self.assertEqual([entry["name"] for entry in screen.settings.camera_entries()],
                         ["Camera 2", "Camera 3", "Gate"])
        self.assertEqual(screen.settings.source, "rtsp://192.0.2.2/live")
        self.assertEqual(screen.settings.live_cameras, [2])  # "Gate" moved from 3 to 2.

    def test_chip_status_follows_engine_stats(self):
        screen = self.screen
        screen._apply_cameras([0, 2])
        with unittest.mock.patch.object(type(self.engine), "running", new=True):
            screen.on_stats({"cameras": [{"position": 0, "status": "CONNECTED", "fps": 25},
                                         {"position": 1, "status": "CAMERA DISCONNECTED", "fps": 0}]})
        self.assertIn("Live", screen.camera_bar.chips[0].toolTip())
        self.assertIn("Offline", screen.camera_bar.chips[2].toolTip())
        self.assertIn("Hidden", screen.camera_bar.chips[1].toolTip())


class CameraDialogTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_password_is_hidden_until_revealed_and_bad_input_is_refused(self):
        from PyQt6.QtWidgets import QLineEdit
        from face_attendance.dashboard.widgets.camera_bar import CameraDialog
        dialog = CameraDialog({"name": "Door", "source": SECRET_URL}, taken_names=["Desk"])
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.source.echoMode(), QLineEdit.EchoMode.Password)
        dialog.reveal.setChecked(True)
        self.assertEqual(dialog.source.echoMode(), QLineEdit.EchoMode.Normal)
        dialog.name.setText("desk")
        dialog._accept()
        self.assertIn("already has this name", dialog.error.text())
        dialog.name.setText("Door")
        dialog.source.setText("192.0.2.1")
        dialog._accept()
        self.assertIn("rtsp://", dialog.error.text())
        dialog.kind.setCurrentIndex(dialog.kind.findData("device"))
        self.assertEqual(dialog.source.echoMode(), QLineEdit.EchoMode.Normal)
        self.assertFalse(dialog.test_button.isVisibleTo(dialog))

    def test_connection_test_runs_off_the_gui_thread(self):
        from face_attendance.dashboard.widgets.camera_bar import CameraDialog
        seen = []

        def probe(source):
            seen.append((source, threading.current_thread() is threading.main_thread()))
            return True, "Connected · 1280 × 720"

        application = _app()
        dialog = CameraDialog({"name": "Door", "source": SECRET_URL}, probe=probe)
        self.addCleanup(dialog.deleteLater)
        dialog._test()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "1280" not in dialog.result.text():
            application.processEvents()
            time.sleep(0.01)
        self.assertEqual(seen, [(SECRET_URL, False)])
        self.assertIn("1280", dialog.result.text())
        self.assertNotIn("s3cret", dialog.result.text())


class SettingsScreenCameraTests(unittest.TestCase):
    def setUp(self):
        from face_attendance.dashboard.screens.settings import SettingsScreen
        self.app = _app()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = Settings(source=SECRET_URL, live_cameras=[0],
                            encodings_path=str(Path(self.directory.name) / "faces.pickle"),
                            employees_path=str(Path(self.directory.name) / "employees.json"),
                            db_path=str(Path(self.directory.name) / "a.db"))
        settings._path = Path(self.directory.name) / "settings.json"
        self.screen = SettingsScreen(Mock(), settings)
        self.addCleanup(self.screen.deleteLater)

    def test_add_edit_remove_cameras(self):
        screen = self.screen
        self.assertEqual(screen.camera_pick.count(), 1)
        self.assertNotIn("s3cret", screen.camera_pick.itemData(0, 3) or "")  # Qt.ToolTipRole
        screen._add_camera()
        screen.camera_name.setText("Back door")
        screen.source_value.setText("rtsp://192.0.2.20/live")
        screen._add_camera()
        screen.camera_name.setText("Desk")
        screen._select_data(screen.source_type, "device")
        screen.source_value.setText("1")
        screen.camera_pick.setCurrentIndex(1)  # Switching keeps what was typed.
        self.assertEqual(screen.source_value.text(), "rtsp://192.0.2.20/live")
        self.assertEqual(screen.source_type.currentData(), "url")
        cameras, live = screen._collect_cameras()
        self.assertEqual([entry["name"] for entry in cameras], ["Camera 1", "Back door", "Desk"])
        self.assertEqual(cameras[0]["source"], SECRET_URL)
        self.assertEqual(live, [0])

        # Removing the first camera makes the next one the main source, and the
        # Live Monitor selection follows the cameras it still refers to.
        self.assertTrue(screen.apply())
        screen.settings.live_cameras = [0, 2]
        screen.load_settings(screen.settings)
        screen.camera_pick.setCurrentIndex(0)
        screen._remove_camera()
        values = screen.collect()
        self.assertEqual(values["source"], "rtsp://192.0.2.20/live")
        self.assertEqual([entry["name"] for entry in values["cameras"]], ["Back door", "Desk"])
        self.assertEqual(values["live_cameras"], [1])

    def test_rtsp_source_is_masked_until_revealed(self):
        from PyQt6.QtWidgets import QLineEdit
        screen = self.screen
        self.assertEqual(screen.source_value.echoMode(), QLineEdit.EchoMode.Password)
        screen._source_reveal.setChecked(True)
        self.assertEqual(screen.source_value.echoMode(), QLineEdit.EchoMode.Normal)
        screen._add_camera()  # A new camera starts hidden again.
        self.assertEqual(screen.source_value.echoMode(), QLineEdit.EchoMode.Password)

    def test_apply_saves_and_reloads(self):
        screen = self.screen
        screen._add_camera()
        screen.camera_name.setText("Lobby")
        screen.source_value.setText("rtsp://192.0.2.30/live")
        self.assertTrue(screen.apply())
        loaded = Settings.load(screen.settings._path)
        self.assertEqual([entry["name"] for entry in loaded.camera_entries()], ["Camera 1", "Lobby"])
        self.assertEqual(loaded.source, SECRET_URL)
        self.assertEqual(screen.camera_pick.count(), 2)

    def test_empty_and_duplicate_cameras_are_refused(self):
        screen = self.screen
        screen._add_camera()
        with self.assertRaisesRegex(ValueError, "Camera 2: enter"):
            screen._collect_cameras()
        screen.source_value.setText("rtsp://192.0.2.40/live")
        screen.camera_name.setText("camera 1")
        with self.assertRaisesRegex(ValueError, "different name"):
            screen._collect_cameras()


if __name__ == "__main__":
    unittest.main()
