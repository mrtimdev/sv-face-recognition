"""Sample photos shipped with the app: the liveness test and the demo employee.

These use the real bundled detector and anti-spoof models.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from face_attendance import samples
from tests.test_dashboard import StubEngine, _app, temp_settings
from tests.test_dashboard_access import wait_until


class SampleTests(unittest.TestCase):
    def test_the_samples_ship_with_their_license(self):
        for name in ("live.jpg", "print.jpg", "screen.jpg", "LICENSE", "NOTICE.md"):
            self.assertTrue((samples.SAMPLES_DIR / name).is_file(), name)
        self.assertIn("face_attendance", str(samples.SAMPLES_DIR))      # bundled with the app assets

    def test_real_face_passes_and_both_photos_are_blocked(self):
        results = samples.check_samples()
        self.assertEqual([result.sample.key for result in results], ["live", "print", "screen"])
        for result in results:
            self.assertEqual(result.error, "", result.sample.key)
            self.assertTrue(result.correct, f"{result.sample.key}: {result.live_score}")
        self.assertTrue(results[0].judged_live)
        self.assertFalse(results[1].judged_live or results[2].judged_live)

    def test_missing_samples_are_reported_not_crashed(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(samples, "SAMPLES_DIR", Path(directory)):
            results = samples.check_samples()
        self.assertTrue(all(result.error and not result.correct for result in results))

    def test_demo_employee_can_be_added_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            catalog, mapping = Path(directory) / "faces.pickle", Path(directory) / "employees.json"
            self.assertFalse(samples.has_demo_employee(catalog))
            outcome = samples.add_demo_employee(catalog, mapping)
            self.assertTrue(outcome.ok, outcome.message)
            self.assertEqual(outcome.employee_id, samples.DEMO_EMPLOYEE_ID)
            self.assertTrue(samples.has_demo_employee(catalog))
            self.assertEqual(samples.remove_demo_employee(catalog, mapping), 1)
            self.assertFalse(samples.has_demo_employee(catalog))


class _Engine(StubEngine):
    def __init__(self):
        super().__init__()
        self.reloads = 0

    def reload_catalog(self):
        self.reloads += 1


class LivenessDialogTests(unittest.TestCase):
    def setUp(self):
        _app()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = temp_settings(temp.name)

    def _dialog(self, engine=None):
        from PyQt6.QtWidgets import QWidget
        from face_attendance.dashboard.screens.liveness import LivenessTestDialog
        host = QWidget()
        host.resize(1200, 900)
        host.show()
        dialog = LivenessTestDialog(self.settings, engine, "light", host)
        dialog.open()
        self.addCleanup(lambda: (dialog.done(0), host.close()))
        self.assertTrue(wait_until(lambda: not dialog.is_busy(), timeout=30))
        _app().processEvents()
        return dialog

    def test_runs_the_check_and_shows_each_result(self):
        dialog = self._dialog()
        self.assertEqual(dialog.verdict_title.text(), "The liveness check works on this computer")
        self.assertEqual(dialog.verdict.property("tone"), "ok")
        texts = [tile.result.label.text() for tile in dialog.tiles]
        self.assertTrue(texts[0].startswith("Passed"), texts)
        self.assertTrue(all(text.startswith("Blocked") for text in texts[1:]), texts)

    def test_a_wrong_result_is_flagged(self):
        from face_attendance.samples import SampleResult
        dialog = self._dialog()
        fooled = [SampleResult(samples.SAMPLES[0], 0.99, True),
                  SampleResult(samples.SAMPLES[1], 0.95, True),         # a photo passed
                  SampleResult(samples.SAMPLES[2], error="model failed")]
        dialog._on_checked(fooled, "")
        self.assertEqual(dialog.verdict.property("tone"), "bad")
        self.assertIn("Printed photo passed (score 0.95)", dialog.verdict_body.text())
        self.assertIn("Phone screen: model failed", dialog.verdict_body.text())
        dialog._on_checked([], "models missing")
        self.assertEqual(dialog.verdict_title.text(), "The liveness check couldn't run")

    def test_demo_employee_toggles_and_reloads_the_engine(self):
        engine = _Engine()
        dialog = self._dialog(engine)
        self.assertEqual(dialog.demo_button.text(), "Add demo employee")
        dialog.demo_button.click()
        self.assertTrue(samples.has_demo_employee(self.settings.encodings_path))
        self.assertEqual((dialog.demo_button.text(), engine.reloads), ("Remove demo employee", 1))
        dialog.demo_button.click()
        self.assertFalse(samples.has_demo_employee(self.settings.encodings_path))
        self.assertEqual((dialog.demo_button.text(), engine.reloads), ("Add demo employee", 2))


if __name__ == "__main__":
    unittest.main()
