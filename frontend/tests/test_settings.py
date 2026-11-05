"""Settings store: persistence, coercion, validation and credential masking."""
import json
import tempfile
import unittest
from pathlib import Path

from face_attendance.config import Config
from face_attendance.settings import Settings, describe_source, mask_url_credentials


class MaskingTests(unittest.TestCase):
    def test_password_is_never_shown(self):
        self.assertEqual(mask_url_credentials("rtsp://admin:s3cret@cam.local/stream"),
                         "rtsp://admin:********@cam.local/stream")

    def test_urls_without_password_pass_through(self):
        self.assertEqual(mask_url_credentials("rtsp://cam.local/stream"),
                         "rtsp://cam.local/stream")
        self.assertEqual(mask_url_credentials("/tmp/cam.mp4"), "/tmp/cam.mp4")

    def test_describe_source_kinds(self):
        self.assertEqual(describe_source("0"), "Webcam 0")
        self.assertEqual(describe_source("rtsp://u:p@h/x"), "rtsp://u:********@h/x")
        self.assertEqual(describe_source("/media/cam.mp4"), "cam.mp4")


class SettingsStoreTests(unittest.TestCase):
    def test_defaults_round_trip_through_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = Settings()
            settings.update({"source": "rtsp://u:p@h", "theme": "light",
                             "face_tolerance": "0.55"})
            self.assertEqual(settings.save(path), path)
            loaded = Settings.load(path)
            self.assertEqual(loaded.to_dict(), settings.to_dict())
            self.assertEqual(loaded.theme, "light")
            self.assertEqual(loaded.face_tolerance, 0.55)

    def test_unknown_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"bogus": 1, "theme": "light"}), encoding="utf-8")
            settings = Settings.load(path)
            self.assertEqual(settings.theme, "light")
            self.assertFalse(hasattr(settings, "bogus"))

    def test_non_object_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                Settings.load(path)

    def test_missing_file_yields_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            self.assertEqual(Settings.load(path).to_dict(), Settings().to_dict())

    def test_update_coerces_strings_and_rejects_garbage(self):
        settings = Settings()
        settings.update({"camera_width": "960", "face_tolerance": 0.4})
        self.assertEqual(settings.camera_width, 960)
        self.assertEqual(settings.face_tolerance, 0.4)
        with self.assertRaises(ValueError):
            settings.update({"camera_width": "wide"})
        settings.update({"not_a_setting": 1})  # unknown keys are silently ignored

    def test_reset_removes_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            Settings().save(path)
            Settings.reset(path)
            self.assertFalse(path.exists())


class ValidationTests(unittest.TestCase):
    def test_attendance_modes_roundtrip_and_reject_invalid(self):
        from face_attendance.config import ATTENDANCE_MODES
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.json"
            for mode in ATTENDANCE_MODES:
                Settings(attendance_mode=mode).save(path)
                settings = Settings.load(path)
                self.assertEqual(settings.to_config().attendance_mode, mode)
                settings.theme = "light"
                settings.save()  # Retains --settings location.
                self.assertEqual(Settings.load(path).theme, "light")
        with self.assertRaises(ValueError):
            Settings(attendance_mode="invalid").to_config()

    def test_defaults_build_a_valid_config(self):
        config = Settings().to_config()
        self.assertIsInstance(config, Config)
        self.assertEqual(config.face_tolerance, Settings().face_tolerance)

    def test_source_is_parsed_into_the_config(self):
        self.assertEqual(Settings(source="2").to_config().source, 2)
        self.assertEqual(Settings(source="rtsp://u:p@h/x").to_config().source,
                         "rtsp://u:p@h/x")

    def test_bad_values_raise_value_error(self):
        with self.assertRaises(ValueError):
            Settings(theme="neon").to_config()
        with self.assertRaises(ValueError):
            Settings(report_page_size=0).to_config()
        with self.assertRaises(ValueError):
            Settings(report_work_start="25:00").to_config()
        with self.assertRaises(ValueError):
            Settings(report_work_start="nine").to_config()

    def test_work_start_accepts_hh_mm_and_empty(self):
        self.assertEqual(Settings(report_work_start="09:30").to_config().face_tolerance,
                         Settings().face_tolerance)
        Settings(report_work_start="").to_config()  # disabled by default


if __name__ == "__main__":
    unittest.main()
