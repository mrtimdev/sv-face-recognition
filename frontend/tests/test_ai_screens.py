"""The AI Configuration settings tab and the AI Face Angles screen."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PyQt6.QtWidgets import QApplication, QFormLayout, QLineEdit, QMessageBox

from face_attendance import ai_providers as ai
from face_attendance.hrm_sync import HRMError
from face_attendance.ai_providers import AIError
from face_attendance.dashboard.screens import ai_angles
from face_attendance.dashboard.screens.ai_angles import AIAnglesScreen, EXISTING, NEW
from face_attendance.dashboard.screens.settings import SettingsScreen
from face_attendance.enrollment import EnrollmentOutcome
from face_attendance.settings import Settings
from face_attendance.storage import write_jpeg

APP = QApplication.instance() or QApplication([])


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
                                 db_path=str(self.root / "a.db"))
        self.settings._path = self.root / "settings.json"


class SettingsTabTests(Base):
    def setUp(self):
        super().setUp()
        self.screen = SettingsScreen(Mock(), self.settings)
        self.addCleanup(self.screen.deleteLater)

    def _choose(self, provider):
        self.screen.ai_provider.setCurrentIndex(self.screen.ai_provider.findData(provider))

    def test_tab_opens(self):
        self.screen.show_ai()
        self.assertEqual(self.screen._stack.currentIndex(), self.screen.AI_TAB)
        self.assertTrue(self.screen._tab_group.button(self.screen.AI_TAB).isChecked())

    def test_all_four_providers_are_offered(self):
        offered = [self.screen.ai_provider.itemData(i) for i in range(self.screen.ai_provider.count())]
        self.assertEqual(offered, ["openai", "gemini", "claude", "custom"])

    def test_switching_provider_keeps_each_key_separate_and_saves_them(self):
        self._choose("openai")
        self.screen.ai_key.setText("open-key")
        self._choose("gemini")
        self.assertEqual(self.screen.ai_key.text(), "")
        self.screen.ai_key.setText("gem-key")
        self._choose("openai")
        self.assertEqual(self.screen.ai_key.text(), "open-key")
        self.screen.settings.update(self.screen.collect())
        profiles = ai.load_profiles(self.screen.settings)
        self.assertEqual((profiles["openai"]["api_key"], profiles["gemini"]["api_key"]), ("open-key", "gem-key"))
        self.assertEqual(self.screen.settings.ai_provider, "openai")

    def test_base_url_shows_for_custom_only(self):
        self.screen.show()
        self.addCleanup(self.screen.hide)
        self.screen.show_ai()
        self._choose("openai")
        self.assertFalse(self.screen.ai_base_url.isVisible())
        self._choose("custom")
        self.assertTrue(self.screen.ai_base_url.isVisible())

    def test_inputs_fill_the_form_width_on_every_platform(self):
        form = next(child for child in self.screen.findChildren(QFormLayout)
                    if child.indexOf(self.screen.ai_provider) >= 0)
        self.assertEqual(form.fieldGrowthPolicy(), QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        long_name = "provider/" + "very-long-model-name-" * 10
        self.screen.ai_chat_model.addItem(long_name)
        self.assertLess(self.screen.ai_chat_model.sizeHint().width(), 500)

    def test_every_settings_form_fills_the_width(self):
        forms = self.screen.findChildren(QFormLayout)
        self.assertGreater(len(forms), 5)
        for form in forms:
            self.assertEqual(form.fieldGrowthPolicy(), QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

    def test_form_hints_and_labels_line_up_with_their_fields(self):
        from PyQt6.QtWidgets import QFormLayout, QLabel

        self.screen.resize(1100, 900)
        self.screen.show()
        self.screen._select_tab(2)
        APP.processEvents()
        form = self.screen._stack.widget(2).findChildren(QFormLayout)[0]
        label = form.itemAt(0, QFormLayout.ItemRole.LabelRole).widget()
        field = form.itemAt(0, QFormLayout.ItemRole.FieldRole).widget()
        hint = form.itemAt(1, QFormLayout.ItemRole.FieldRole).widget()
        self.assertIsInstance(hint, QLabel)
        self.assertEqual(hint.height(), hint.heightForWidth(hint.width()))
        self.assertEqual(label.height(), field.height())
        self.assertLessEqual(hint.y() - field.geometry().bottom(), 10)

    def test_loaded_models_can_be_selected_from_the_list(self):
        combo = self.screen.ai_chat_model
        self.screen._ai_fill_models(combo, ["model-a", "model-b", "model-c"], "model-b")
        self.assertEqual(combo.currentText(), "model-b")
        self.assertEqual(combo.count(), 3)
        combo.setCurrentIndex(2)
        self.assertEqual(combo.currentText(), "model-c")
        self.assertEqual(self.screen._ai_config().chat_model, "model-c")

    def test_first_click_on_a_model_field_opens_the_list(self):
        from PyQt6.QtCore import QEvent, QPoint, Qt
        from PyQt6.QtGui import QFocusEvent
        from PyQt6.QtTest import QTest
        combo = self.screen.ai_chat_model
        combo.addItems(["model-a", "model-b"])
        self.screen.show()
        self.screen.show_ai()
        APP.processEvents()
        click = lambda: QTest.mouseClick(combo.lineEdit(), Qt.MouseButton.LeftButton, pos=QPoint(20, 10))
        # Test windows are never active here, so deliver the focus a real click would give.
        combo.focusInEvent(QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason))
        click()
        APP.processEvents()
        self.assertTrue(combo.view().isVisible())
        combo.hidePopup()
        APP.processEvents()
        # Once the field has focus a click places the cursor, so a name can be edited.
        click()
        APP.processEvents()
        self.assertFalse(combo.view().isVisible())

    def test_typing_filters_models_anywhere_in_the_name(self):
        combo = self.screen.ai_chat_model
        combo.addItems(["antigravity/claude-opus", "openai/gpt-5", "Other/GPT-mini"])
        finder = combo.completer()
        finder.setCompletionPrefix("gpt")
        self.assertEqual(finder.completionCount(), 2)

    def test_opening_an_empty_list_says_to_load_models(self):
        self.assertEqual(self.screen.ai_chat_model.count(), 0)
        self.screen.ai_chat_model.showPopup()
        self.assertIn("Load models", self.screen.ai_load_result.text())

    def test_a_provider_with_no_models_for_a_field_leaves_it_empty(self):
        self.screen._ai_fill_models(self.screen.ai_image_model, [])
        self.assertEqual(self.screen.ai_image_model.currentText(), "")

    def test_reveal_shows_and_hides_a_secret(self):
        self.screen.ai_show_key.setChecked(True)
        self.assertEqual(self.screen.ai_key.echoMode(), QLineEdit.EchoMode.Normal)
        self._choose("gemini")  # Switching provider hides the key again.
        self.assertEqual(self.screen.ai_key.echoMode(), QLineEdit.EchoMode.Password)

    def test_turning_sounds_off_dims_the_sound_options_and_keeps_their_values(self):
        self.screen.sounds_master.setChecked(False)
        self.assertFalse(self.screen.fields["sound_error"].isEnabled())
        self.assertTrue(self.screen.collect()["sound_error"])
        self.screen.sounds_master.setChecked(True)
        self.assertTrue(self.screen.fields["sound_error"].isEnabled())

    def test_every_field_round_trips_through_the_redesigned_form(self):
        self.settings.camera_width = 1920
        self.settings.reconnect_sec = 2.5
        self.settings.cooldown_sec = 120
        self.settings.telegram_notify_unknown = False
        self.settings.auto_capture_inactive = False
        self.screen.load_settings(self.settings)
        values = self.screen.collect()
        self.assertEqual(values["camera_width"], 1920)
        self.assertEqual(values["reconnect_sec"], 2.5)
        self.assertEqual(values["cooldown_sec"], 120)
        self.assertFalse(values["telegram_notify_unknown"])
        self.assertFalse(values["auto_capture_inactive"])
        self.assertEqual(self.screen.fields["camera_width"].suffix(), " px")

    def test_options_removed_from_the_screen_keep_their_saved_values(self):
        saved = {"opencv_threads": 4, "persistence_queue_size": 16, "encodings_path": "/data/faces.pickle",
                 "employees_path": "/data/staff.json", "log_path": "/data/seen.csv"}
        self.settings.update(saved)
        self.screen.load_settings(self.settings)
        self.assertFalse(saved.keys() & self.screen.fields.keys())
        self.assertTrue(self.screen.apply())
        reloaded = Settings.load(self.settings._path)
        self.assertEqual({key: getattr(reloaded, key) for key in saved}, saved)

    def test_plain_http_address_shows_a_warning(self):
        self._choose("custom")
        self.screen.ai_base_url.setText("http://45.201.196.19:3003/v1")
        self.assertIn("unencrypted", self.screen.ai_url_warning.text())
        self.screen.ai_base_url.setText("https://gateway.example/v1")
        self.assertEqual(self.screen.ai_url_warning.text(), "")

    def test_claude_is_marked_as_having_no_image_generation(self):
        self._choose("claude")
        self.assertIn("can't generate images", self.screen.ai_provider_note.text())
        self.assertFalse(self.screen.ai_image_model.isEnabled())

    def test_load_models_fills_both_lists(self):
        self._choose("openai")
        self.screen.ai_key.setText("k")
        with patch("face_attendance.dashboard.screens.settings.list_models",
                   return_value=["gpt-x", "gpt-image-y"]):
            self.screen.ai_load_button.click()
            self.assertTrue(pump(lambda: self.screen.ai_load_button.isEnabled()))
        self.assertEqual(self.screen.ai_chat_model.currentText(), "gpt-x")
        self.assertEqual(self.screen.ai_image_model.currentText(), "gpt-image-y")
        self.assertIn("2 models", self.screen.ai_load_result.text())

    def test_claude_prefers_the_current_default_model(self):
        self._choose("claude")
        self.screen.ai_key.setText("k")
        with patch("face_attendance.dashboard.screens.settings.list_models",
                   return_value=["claude-haiku-5-5", "claude-opus-5-5"]):
            self.screen.ai_load_button.click()
            self.assertTrue(pump(lambda: self.screen.ai_load_button.isEnabled()))
        self.assertEqual(self.screen.ai_chat_model.currentText(), "claude-opus-5-5")

    def test_loading_keeps_a_model_already_chosen(self):
        self._choose("openai")
        self.screen.ai_key.setText("k")
        self.screen.ai_chat_model.setCurrentText("my-model")
        with patch("face_attendance.dashboard.screens.settings.list_models", return_value=["gpt-x"]):
            self.screen.ai_load_button.click()
            self.assertTrue(pump(lambda: self.screen.ai_load_button.isEnabled()))
        self.assertEqual(self.screen.ai_chat_model.currentText(), "my-model")

    def test_test_message_shows_the_reply_and_the_error(self):
        self._choose("openai")
        self.screen.ai_key.setText("k")
        self.screen.ai_chat_model.setCurrentText("gpt-x")
        self.screen.ai_test_input.setText("Ping")
        with patch("face_attendance.dashboard.screens.settings.send_test_message",
                   return_value="Pong") as send:
            self.screen.ai_test_button.click()
            self.assertTrue(pump(lambda: self.screen.ai_test_button.isEnabled()))
        self.assertEqual(send.call_args.args[1], "Ping")
        self.assertIn("Pong", self.screen.ai_test_result.text())
        with patch("face_attendance.dashboard.screens.settings.send_test_message",
                   side_effect=AIError("The provider rejected the API key.", 401)):
            self.screen.ai_test_button.click()
            self.assertTrue(pump(lambda: self.screen.ai_test_button.isEnabled()))
        self.assertIn("rejected", self.screen.ai_test_result.text())

    def test_result_from_a_previous_provider_is_ignored(self):
        self._choose("openai")
        self.screen.ai_key.setText("k")
        with patch("face_attendance.dashboard.screens.settings.list_models", return_value=["gpt-x"]):
            self.screen.ai_load_button.click()
            self._choose("gemini")
            self.assertTrue(pump(lambda: self.screen.ai_load_button.isEnabled()))
        self.assertEqual(self.screen.ai_chat_model.count(), 0)


class FakeChecker:
    """Stands in for the real face check, whose models can't make sense of the blank test images."""

    def __init__(self, result, source_problem=""):
        self._result = result
        self._source_problem = source_problem

    def source_problem(self):
        return self._source_problem

    def __call__(self, _content):
        return self._result()


class AnglesScreenTests(Base):
    def setUp(self):
        identity = patch("face_attendance.identity_check.validate_hrm_upload")
        self.identity_check = identity.start()
        self.addCleanup(identity.stop)
        super().setUp()
        self.settings.ai_provider = "custom"
        self.settings.ai_profiles = ai.dump_profiles({"custom": {
            "api_key": "k", "base_url": "https://g.example/v1", "image_model": "img"}})
        self.engine = Mock()
        self.screen = AIAnglesScreen(self.engine, self.settings)
        self.addCleanup(self.screen.deleteLater)
        self.photo = np.full((80, 80, 3), 100, np.uint8)
        # The fake images are blank, so by default leave the real face check out of these tests.
        self.checks = {}
        self.source_problem = ""
        checker = patch.object(ai_angles.angle_templates, "generated_image_checker",
                               side_effect=lambda _jpeg: FakeChecker(self._next_check, self.source_problem))
        checker.start()
        self.addCleanup(checker.stop)

    def _next_check(self):
        return self.checks.pop("next", "matched") if len(self.checks) else "matched"

    def _new_person(self, name="Jane Smith"):
        self.screen.new_button.setChecked(True)
        self.screen._mode_changed(NEW)
        self.screen.names.full.setText(name)
        self.screen._set_source(self.photo)
        self.screen._update_buttons()

    def _fake_generate(self, failing=()):
        def generate(_config, _source, angle, directory):
            if angle.key in failing:
                raise AIError("No image this time")
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{angle.key}.jpg"
            write_jpeg(path, self.photo)
            return path
        return patch.object(ai_angles, "generate_angle_file", side_effect=generate)

    def test_a_generated_image_that_fails_the_face_check_warns_on_its_tile_and_in_the_message(self):
        self._new_person()
        self.checks["next"] = "mismatch"  # Only the first image generated.
        with self._fake_generate():
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        first, *others = self.screen.tiles.values()
        self.assertFalse(first.warning.isHidden())
        self.assertIn("different person", first.warning.text())
        self.assertFalse(first.include.isChecked())
        self.assertFalse(first.include.isEnabled())  # A mismatch can't be enrolled at all.
        self.assertTrue(all(tile.warning.isHidden() and tile.include.isChecked() for tile in others))
        self.assertIn("1 of the angles don't match the profile photo", self.screen.toast.label.text())
        self.assertIn("Verified 83% · Warning 17%", self.screen.match_label.text())
        self.assertIn("only the 5 verified", self.screen.match_label.text())
        self.assertNotIn(first.path, self.screen._included_paths())

    def test_a_photo_with_no_clear_face_costs_no_ai_request(self):
        self._new_person()
        self.source_problem = "no_face"
        with self._fake_generate() as generate:
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        generate.assert_not_called()
        self.assertIn("Nothing was generated. No face found", self.screen.toast.label.text())

    def test_an_angle_that_does_not_match_is_never_sent_to_hrm(self):
        self._with_hrm()
        self._new_person()
        self.screen.id_edit.setText("EMP002")
        self.checks["next"] = "profile_mismatch"
        with self._fake_generate(), patch.object(ai_angles, "upload_angle", return_value={}) as upload:
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        first = next(iter(self.screen.tiles))
        self.assertEqual(upload.call_count, 5)
        self.assertNotIn(first, [call.args[2] for call in upload.call_args_list])
        tile = self.screen.tiles[first]
        self.assertFalse(tile.hrm_sent)
        self.assertEqual(tile.status.text(), "Ready · not sent to HRM")
        self.assertIn("profile photo", tile.warning.text())

    def test_send_to_hrm_leaves_out_angles_that_do_not_match(self):
        self._with_hrm(auto=False)
        self._new_person()
        self.screen.id_edit.setText("EMP002")
        self.checks["next"] = "profile_mismatch"
        with self._fake_generate():
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        with patch.object(ai_angles, "upload_angle", return_value={}) as upload:
            self.screen.hrm_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertEqual(upload.call_count, 5)
        for tile in self.screen.tiles.values():
            tile.set_check("mismatch")
            tile.hrm_sent = False
        with patch.object(ai_angles, "upload_angle") as upload:
            self.screen.hrm_button.click()
        upload.assert_not_called()
        self.assertIn("None of these angles match", self.screen.toast.label.text())

    def test_the_summary_says_when_all_six_match(self):
        self._new_person()
        self.assertIn("Verified 0% · Warning 0%", self.screen.match_label.text())
        with self._fake_generate():
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertIn("Verified 100%", self.screen.match_label.text())
        self.assertIn("matches all 6", self.screen.match_label.text())

    def test_the_summary_says_when_the_monitor_will_not_recognize_someone(self):
        self._new_person()
        with self._fake_generate():
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        for key in list(self.screen.tiles)[:3]:
            self.screen.tiles[key].set_check("profile_mismatch")
        self.screen._update_buttons()
        self.assertIn("Verified 50% · Warning 50%", self.screen.match_label.text())
        self.assertIn("won't recognize", self.screen.match_label.text())

    def _existing_employee(self, in_hrm=True):
        self._with_hrm()
        row = {"employee_id": "EMP001", "employee_name": "Jane Smith", "name": "", "in_hrm": in_hrm}
        self.screen.employee_combo.blockSignals(True)
        self.screen.employee_combo.addItem("Jane Smith (EMP001)", row)
        self.screen.employee_combo.setCurrentIndex(self.screen.employee_combo.count() - 1)
        self.screen.employee_combo.blockSignals(False)
        patcher = patch.object(self.screen, "_load_saved_angles")
        self.load_angles = patcher.start()
        self.addCleanup(patcher.stop)
        fetch = patch.object(ai_angles.hd, "fetch_photo", return_value=None)
        fetch.start()
        self.addCleanup(fetch.stop)
        path = self.root / "new.jpg"
        write_jpeg(path, self.photo)
        return str(path)

    def _drop(self, path, check="matched", upload=None):
        with patch.object(ai_angles.angle_templates, "check_new_profile_photo", return_value=check), \
                patch.object(ai_angles, "upload_profile_photo", **(upload or {})) as saved:
            self.screen.preview.dropped.emit(path)
            self.assertTrue(pump(lambda: not self.screen.busy))
        return saved

    def test_duplicate_profile_cannot_be_uploaded_even_after_confirmation(self):
        self.identity_check.side_effect = HRMError("This face matches employee EMP002.")
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes):
            saved = self._drop(self._existing_employee(), check="mismatch")
        saved.assert_not_called()
        self.assertIn("EMP002", self.screen.photo_status.text())

    def test_duplicate_generated_angles_are_never_uploaded(self):
        self._with_hrm()
        self._new_person()
        self.screen.id_edit.setText("EMP002")
        self.identity_check.side_effect = HRMError("This face matches employee EMP001.")
        with self._fake_generate(), patch.object(ai_angles, "upload_angle") as upload:
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        upload.assert_not_called()
        self.assertTrue(self.identity_check.called)

    def test_a_dropped_photo_is_saved_to_the_profile_straight_away(self):
        saved = self._drop(self._existing_employee())
        saved.assert_called_once()
        self.assertEqual(saved.call_args.args[1], "EMP001")
        self.assertTrue(saved.call_args.args[2].startswith(b"\xff\xd8"))
        self.assertIn("Saved to the employee's profile", self.screen.photo_status.text())
        self.assertFalse(self.screen._uploaded)  # HRM's updates apply again.
        self.load_angles.assert_called()  # The saved angles are checked against the new photo.

    def test_a_photo_with_no_face_is_not_saved(self):
        saved = self._drop(self._existing_employee(), check="no_face")
        saved.assert_not_called()
        self.assertIn("Not saved to the profile. No face found", self.screen.photo_status.text())
        self.assertTrue(self.screen._uploaded)

    def test_another_persons_photo_asks_first(self):
        path = self._existing_employee()
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes) as ask:
            saved = self._drop(path, check="mismatch")
        ask.assert_called_once()
        saved.assert_called_once()
        self.assertIn("Saved", self.screen.photo_status.text())

    def test_declining_another_persons_photo_goes_back_to_the_profile_photo(self):
        path = self._existing_employee()
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No), \
                patch.object(self.screen, "_use_profile_photo") as back:
            saved = self._drop(path, check="mismatch")
        saved.assert_not_called()
        back.assert_called_once()
        self.assertFalse(self.screen._uploaded)

    def test_a_failed_save_says_why_and_keeps_the_photo(self):
        saved = self._drop(self._existing_employee(), upload={"side_effect": HRMError("HRM is down")})
        saved.assert_called_once()
        self.assertIn("Not saved to the profile. HRM is down", self.screen.photo_status.text())
        self.assertIsNotNone(self.screen.source)
        self.assertTrue(self.screen._uploaded)

    def test_someone_not_in_hrm_has_no_profile_to_save_to(self):
        saved = self._drop(self._existing_employee(in_hrm=False))
        saved.assert_not_called()
        self.assertIn("isn't in SV HRM", self.screen.photo_status.text())

    def test_a_new_employee_photo_is_not_saved_anywhere(self):
        self._with_hrm()
        self._new_person()
        path = self.root / "new.jpg"
        write_jpeg(path, self.photo)
        saved = self._drop(str(path))
        saved.assert_not_called()
        self.assertTrue(self.screen.photo_status.isHidden())

    def _fitted(self, width, height):
        self.screen.resize(width, height)
        self.screen.show()
        pump(lambda: False, 0.1)
        self.screen._fit_tiles()
        pump(lambda: False, 0.1)
        return next(iter(self.screen.tiles.values()))

    def test_the_cards_grow_and_shrink_with_the_window(self):
        small = self._fitted(1000, 620).image.width()
        large = self._fitted(1700, 1300).image.width()
        self.assertEqual(small, ai_angles.MIN_TILE_IMAGE)
        self.assertGreater(large, 250)
        self.assertLessEqual(large, ai_angles.MAX_TILE_IMAGE)
        self.assertEqual(self._fitted(1000, 620).image.width(), small)

    def test_three_cards_fill_the_container_width(self):
        tile = self._fitted(1700, 900)
        tiles = list(self.screen.tiles.values())
        widths = [t.width() for t in tiles[:3]]
        self.assertLessEqual(max(widths) - min(widths), 1)  # Equal, give or take a rounded pixel.
        left_gap = tiles[0].x()
        right_gap = self.screen.angles_card.width() - (tiles[2].x() + tiles[2].width())
        self.assertLessEqual(abs(left_gap - right_gap), 1)  # Nothing is left empty at the right.
        self.assertEqual(tile.image.width(), tile.image.height())

    def test_both_rows_fit_without_scrolling_when_the_window_has_room(self):
        self._fitted(1280, 960)
        bar = self.screen.angles_scroll.verticalScrollBar()
        self.assertEqual(bar.maximum(), 0)

    def test_the_size_settles_instead_of_changing_each_time(self):
        first = self._fitted(1280, 860).image.width()
        for _ in range(3):
            self.screen._fit_tiles()
            pump(lambda: False, 0.03)
        self.assertEqual(next(iter(self.screen.tiles.values())).image.width(), first)

    def test_redo_clears_the_old_warning(self):
        self._new_person()
        with self._fake_generate():
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        tile = next(iter(self.screen.tiles.values()))
        tile.set_check("no_face")
        self.assertFalse(tile.warning.isHidden())
        with self._fake_generate():
            tile.retry.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertTrue(tile.warning.isHidden())

    def test_generating_fills_all_six_tiles_in_one_folder(self):
        self._new_person()
        with self._fake_generate():
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        paths = [tile.path for tile in self.screen.tiles.values()]
        self.assertEqual(len(paths), 6)
        self.assertTrue(all(Path(path).is_file() for path in paths))
        self.assertEqual(len({Path(path).parent for path in paths}), 1)
        self.assertIn("ai_angles", paths[0])
        self.assertTrue(self.screen.enroll_button.isEnabled())

    def test_one_failure_does_not_stop_the_others_and_redo_replaces_it(self):
        self._new_person()
        with self._fake_generate(failing={"up"}):
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertIsNone(self.screen.tiles["up"].path)
        self.assertIn("No image", self.screen.tiles["up"].status.text())
        self.assertEqual(sum(1 for tile in self.screen.tiles.values() if tile.path), 5)
        folder = self.screen.output_dir
        with self._fake_generate() as generate:
            self.screen.tiles["up"].retry.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertEqual(generate.call_count, 1)
        self.assertTrue(self.screen.tiles["up"].path)
        self.assertEqual(self.screen.output_dir, folder)

    def _with_hrm(self, auto=True):
        self.settings.hrm_api_url = "https://hrm.example.com"
        self.settings.hrm_api_key = "k" * 40
        self.settings.hrm_auto_upload = auto

    def test_each_generated_angle_goes_to_hrm_for_the_employee_id(self):
        self._with_hrm()
        self._new_person()
        self.screen.id_edit.setText("EMP002")
        with self._fake_generate(), patch.object(ai_angles, "upload_angle", return_value={}) as upload:
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertEqual([c.args[2] for c in upload.call_args_list], [a.key for a in ai.ANGLES])
        self.assertTrue(all(c.args[1] == "EMP002" for c in upload.call_args_list))
        self.assertTrue(all(tile.hrm_sent for tile in self.screen.tiles.values()))
        self.assertIn("HRM", self.screen.toast.label.text())

    def test_rejected_hrm_key_stops_uploads_but_not_generation(self):
        self._with_hrm()
        self._new_person()
        self.screen.id_edit.setText("EMP002")
        with self._fake_generate(), patch.object(ai_angles, "upload_angle",
                                                 side_effect=HRMError("HRM rejected the API key.", 401)) as upload:
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(sum(1 for tile in self.screen.tiles.values() if tile.path), 6)
        self.assertFalse(any(tile.hrm_sent for tile in self.screen.tiles.values()))

    def test_no_upload_without_an_employee_id_or_when_auto_upload_is_off(self):
        self._with_hrm()
        self._new_person()
        with self._fake_generate(), patch.object(ai_angles, "upload_angle") as upload:
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        upload.assert_not_called()
        self._with_hrm(auto=False)
        self.screen.id_edit.setText("EMP002")
        with self._fake_generate(), patch.object(ai_angles, "upload_angle") as upload:
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        upload.assert_not_called()

    def test_send_to_hrm_uploads_the_ready_angles(self):
        self._with_hrm(auto=False)
        self._new_person()
        self.screen.id_edit.setText("EMP002")
        with self._fake_generate(failing={"blink"}):
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        with patch.object(ai_angles, "upload_angle", return_value={}) as upload:
            self.screen.hrm_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertEqual(upload.call_count, 5)
        self.assertIn("stored all 5", self.screen.toast.label.text())

    def test_rejected_key_stops_the_run(self):
        self._new_person()
        with patch.object(ai_angles, "generate_angle_file", side_effect=AIError("Bad key", 401)) as generate:
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        self.assertEqual(generate.call_count, 1)

    def test_nothing_is_sent_without_person_photo_or_provider(self):
        with self._fake_generate() as generate:
            self.screen.new_button.setChecked(True)
            self.screen._mode_changed(NEW)
            self.screen.generate_button.click()  # no name
            self.assertIn("full name", self.screen.toast.label.text())
            self.screen.names.full.setText("Jane")
            self.screen.generate_button.click()  # no photo
            self.assertIn("photo", self.screen.toast.label.text())
            self.settings.ai_provider = "claude"
            self.screen._set_source(self.photo)
            self.screen.generate_button.click()  # Claude can't generate
            self.assertIn("can't generate", self.screen.toast.label.text())
            self.assertFalse(self.screen.busy)
        generate.assert_not_called()

    def test_changing_the_photo_clears_earlier_results(self):
        self._new_person()
        with self._fake_generate():
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))
        path = self.root / "other.jpg"
        write_jpeg(path, self.photo)
        self.screen._load_photo(str(path))
        self.assertFalse(any(tile.path for tile in self.screen.tiles.values()))
        self.assertIsNone(self.screen.output_dir)

    def test_employee_who_is_not_in_hrm_uses_their_enrolled_photo(self):
        row = {"name": "Jane", "employee_name": "Jane", "employee_id": "E1", "active": True}
        photo = self.root / "enrolled.jpg"
        write_jpeg(photo, self.photo)
        with patch.object(self.screen.service, "employees", return_value=[row]), \
                patch.object(ai_angles, "sample_photo_map", return_value={"Jane": [photo]}):
            self.screen._reload_employees()
            self.assertIsNone(self.screen.source)  # Nothing is picked for the user.
            self.screen.employee_combo.setCurrentIndex(0)
        self.assertIsNotNone(self.screen.source)
        self.assertEqual(self.screen._person_label(), "Jane")

    def test_employee_without_a_photo_is_told_to_upload(self):
        row = {"name": "Jane", "employee_name": "Jane", "employee_id": "E1", "active": True}
        with patch.object(self.screen.service, "employees", return_value=[row]), \
                patch.object(ai_angles, "sample_photo_map", return_value={}):
            self.screen._reload_employees()
            self.screen.employee_combo.setCurrentIndex(0)
        self.assertIsNone(self.screen.source)
        self.assertIn("Upload", self.screen.toast.label.text())

    def _generated(self):
        with self._fake_generate():
            self.screen.generate_button.click()
            self.assertTrue(pump(lambda: not self.screen.busy))

    def test_hrm_duplicate_blocks_local_ai_enrollment_before_any_samples_are_saved(self):
        self._with_hrm()
        self.identity_check.side_effect = HRMError("This face matches employee EMP002.")
        with patch.object(self.screen.service, "enroll_many") as enroll:
            self.screen._submit_enrollment([self.photo], "Jane", "EMP001", {})
            self.assertTrue(pump(lambda: not self.screen._saving))
        enroll.assert_not_called()
        self.assertIn("EMP002", self.screen.toast.label.text())

    def test_enrollment_requires_confirmation(self):
        self._new_person()
        self._generated()
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.No), \
                patch.object(self.screen.runner, "submit") as submit:
            self.screen.enroll_button.click()
        submit.assert_not_called()

    def test_new_employee_enrolls_the_real_photo_first_and_only_checked_images(self):
        self._new_person()
        self._generated()
        self.screen.tiles["blink"].include.setChecked(False)
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(self.screen.runner, "submit") as submit:
            self.screen.enroll_button.click()
        images, name, employee_id, quality, *_ = submit.call_args.args
        self.assertEqual(len(images), 6)  # real photo + five checked
        self.assertIs(images[0], self.photo)
        self.assertEqual((name, employee_id, quality), ("Jane Smith", "", True))

    def test_new_employee_cannot_reuse_an_existing_name(self):
        row = {"name": "Jane Smith", "employee_name": "Jane Smith", "employee_id": "E1", "active": True}
        with patch.object(self.screen.service, "employees", return_value=[row]):
            self.screen._reload_employees()
        self._new_person()
        self._generated()
        with patch.object(QMessageBox, "warning") as warning, patch.object(self.screen.runner, "submit") as submit:
            self.screen.enroll_button.click()
        warning.assert_not_called()
        submit.assert_not_called()
        self.assertIn("already exists", self.screen.toast.label.text())

    def test_existing_employee_adds_generated_images_only(self):
        row = {"name": "Jane", "employee_name": "Jane", "employee_id": "E1", "active": True}
        photo = self.root / "enrolled.jpg"
        write_jpeg(photo, self.photo)
        with patch.object(self.screen.service, "employees", return_value=[row]), \
                patch.object(ai_angles, "sample_photo_map", return_value={"Jane": [photo]}):
            self.screen._reload_employees()
            self.screen.employee_combo.setCurrentIndex(0)
        self.assertEqual(self.screen.person_stack.currentIndex(), EXISTING)
        self._generated()
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(self.screen.runner, "submit") as submit:
            self.screen.enroll_button.click()
        images, name, employee_id, *_ = submit.call_args.args
        self.assertEqual((len(images), name, employee_id), (6, "Jane", "E1"))

    def test_enrollment_result_reloads_the_catalog_only_on_success(self):
        self.screen._on_enrolled(EnrollmentOutcome("no_face", "No face found"))
        self.engine.reload_catalog.assert_not_called()
        self.assertIn("No face", self.screen.toast.label.text())
        self.screen._on_enrolled(EnrollmentOutcome("enrolled", "Enrolled Jane", name="Jane", total_samples=7))
        self.engine.reload_catalog.assert_called_once()

    def test_screen_names_the_host_that_receives_the_photo(self):
        self.assertIn("g.example", self.screen.privacy_label.text())

    def test_settings_change_refreshes_the_provider_line(self):
        self.assertIn("img", self.screen.provider_label.text())
        self.settings.ai_provider = "claude"
        self.screen.on_settings_changed(self.settings)
        self.assertIn("can't generate", self.screen.provider_label.text())


if __name__ == "__main__":
    unittest.main()
