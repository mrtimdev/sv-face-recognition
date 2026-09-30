"""Sign-in, header account/notification menus and the Users & Access forms.

Qt runs offscreen.  Accounts live in a temporary SQLite database, so the real
``UserRepository`` (and its password hashing) is exercised end to end.
"""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from tests.test_dashboard import _app, temp_settings


def wait_until(predicate, timeout=6.0):
    """Spin the event loop until *predicate* holds (worker threads, timers)."""
    from PyQt6.QtCore import QEventLoop
    application = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _TempAccounts:
    """Temp settings plus a repository seeded with an administrator."""

    def setUp(self):
        _app()
        self._dir = tempfile.TemporaryDirectory()
        self.settings = temp_settings(self._dir.name)
        from face_attendance.users import UserRepository
        self.repo = UserRepository(self.settings)
        self.admin = self.repo.create_user("sokha", "Sokha Chan", "+855 12 345 678",
                                           "sokha@example.com", "secret1", "admin")

    def tearDown(self):
        _app().processEvents()
        self._dir.cleanup()


class SignInDialogTests(_TempAccounts, unittest.TestCase):
    def _dialog(self):
        from face_attendance.dashboard.login import UserLoginDialog
        dialog = UserLoginDialog(self.settings, version="v-test")
        dialog.show()
        return dialog

    def test_successful_sign_in_is_staged_and_returns_the_user(self):
        from PyQt6.QtWidgets import QDialog
        dialog = self._dialog()
        dialog.username.setText("sokha")
        dialog.password.setText("secret1")
        dialog.remember_check.setChecked(False)
        dialog._submit()
        # Work happens off the GUI thread while the form is locked.
        self.assertTrue(dialog.is_busy())
        self.assertTrue(dialog.submit.is_loading())
        self.assertFalse(dialog.username.isEnabled())
        self.assertEqual(dialog.status.text(), "Connecting to the user database…")
        self.assertTrue(wait_until(lambda: dialog.status.text().startswith("Verifying")))
        self.assertTrue(wait_until(lambda: dialog.result() == QDialog.DialogCode.Accepted))
        self.assertEqual(dialog.current_user.uuid, self.admin.uuid)
        self.assertFalse(dialog.remember)
        self.assertEqual(dialog.submit.text(), "Welcome back, Sokha")
        self.assertEqual(dialog.progress.tone(), "ok")

    def test_empty_fields_are_flagged_inline_without_starting_work(self):
        from PyQt6.QtTest import QTest
        dialog = self._dialog()
        dialog._submit()
        self.assertFalse(dialog.is_busy())
        self.assertTrue(dialog.username_field.has_error)
        self.assertTrue(dialog.password_field.has_error)
        self.assertEqual(dialog.username_field.help_label.text(), "Enter your username.")
        QTest.keyClicks(dialog.username, "s")
        self.assertFalse(dialog.username_field.has_error)
        self.assertTrue(dialog.password_field.has_error)
        dialog.close()

    def test_wrong_password_counts_attempts_then_locks_out(self):
        from PyQt6.QtWidgets import QDialog
        with patch("face_attendance.dashboard.login.STAGE_MIN_MS", 1), \
                patch("face_attendance.dashboard.login.LOCKOUT_CLOSE_MS", 10):
            dialog = self._dialog()
            dialog.username.setText("sokha")
            dialog.password.setText("wrong")
            dialog._submit()
            self.assertTrue(wait_until(lambda: not dialog.is_busy()))
            self.assertEqual(dialog.attempts, 1)
            self.assertEqual(dialog.alert.text(), "Incorrect username or password. 4 attempts left.")
            self.assertEqual(dialog.password.text(), "")
            self.assertTrue(dialog.password.isEnabled())
            self.assertEqual(dialog.progress.tone(), "bad")
            for attempt in range(2, 6):
                dialog.password.setText("wrong")
                dialog._submit()
                self.assertTrue(wait_until(lambda: dialog.attempts == attempt))
            self.assertIn("Too many failed attempts", dialog.alert.text())
            self.assertTrue(wait_until(lambda: dialog.result() == QDialog.DialogCode.Rejected
                                       and not dialog.isVisible()))
            self.assertIsNone(dialog.current_user)

    def test_database_errors_are_reported_without_using_an_attempt(self):
        from face_attendance.users import UserRepository
        with patch.object(UserRepository, "authenticate", side_effect=RuntimeError("connection refused")):
            dialog = self._dialog()
            dialog.username.setText("sokha")
            dialog.password.setText("secret1")
            dialog._submit()
            self.assertTrue(wait_until(lambda: not dialog.is_busy()))
        self.assertEqual(dialog.attempts, 0)
        self.assertEqual(dialog.alert.text(), "Couldn't reach the user database. connection refused")
        self.assertTrue(dialog.submit.isEnabled())
        dialog.close()

    def test_closing_mid_sign_in_ignores_the_late_result(self):
        from face_attendance.dashboard import threads
        dialog = self._dialog()
        dialog.username.setText("sokha")
        dialog.password.setText("secret1")
        dialog._submit()
        dialog.reject()
        self.assertTrue(wait_until(lambda: not threads.is_parked(dialog._worker)))
        wait_until(lambda: False, timeout=0.3)
        self.assertIsNone(dialog.current_user)
        self.assertFalse(dialog.isVisible())

    def test_pin_dialog_unlocks_only_with_the_right_pin(self):
        from PyQt6.QtWidgets import QDialog
        from face_attendance.dashboard.login import LoginDialog
        from face_attendance.settings import hash_pin
        with patch("face_attendance.dashboard.login.STAGE_MIN_MS", 1):
            dialog = LoginDialog(hash_pin("2468"))
            dialog.show()
            dialog.pin.setText("1111")
            dialog._submit()
            self.assertTrue(wait_until(lambda: dialog.attempts == 1))
            self.assertEqual(dialog.alert.text(), "Incorrect PIN. 4 attempts left.")
            dialog.pin.setText("2468")
            dialog._submit()
            self.assertTrue(wait_until(lambda: dialog.result() == QDialog.DialogCode.Accepted))


class NotificationCenterTests(unittest.TestCase):
    def setUp(self):
        _app()

    def test_badge_counts_unseen_and_rows_stay_unread_until_closed(self):
        from face_attendance.dashboard.widgets import NotificationCenter
        center = NotificationCenter()
        center.add("checkin", "Sokha Chan", "Checked in")
        center.add("unknown", "Unknown face detected", "Track #2")
        self.assertEqual((center.unseen_count(), center.unread_count()), (2, 2))
        center.mark_seen()
        self.assertEqual((center.unseen_count(), center.unread_count()), (0, 2))
        first = center.items()[-1]
        center.mark_read(first.uid)
        self.assertEqual(center.unread_count(), 1)
        center.mark_read()
        self.assertEqual(center.unread_count(), 0)
        self.assertEqual([item.category for item in center.items()], ["alerts", "checkins"])
        self.assertEqual(len(center.items("checkins")), 1)
        center.clear()
        self.assertEqual(len(center), 0)

    def test_repeated_alerts_collapse_but_check_ins_never_do(self):
        from face_attendance.dashboard.widgets import NotificationCenter
        center = NotificationCenter()
        now = datetime.now()
        center.add("error", "Engine error", "Camera lost", when=now)
        center.mark_read()
        repeat = center.add("error", "Engine error", "Camera lost", when=now + timedelta(seconds=30))
        self.assertEqual(len(center), 1)
        self.assertEqual(repeat.repeat, 2)
        self.assertFalse(repeat.read)
        center.add("error", "Engine error", "Camera lost", when=now + timedelta(minutes=30))
        self.assertEqual(len(center), 2)
        for _ in range(2):
            center.add("checkin", "Dara", "Checked in", when=now)
        self.assertEqual(len(center), 4)

    def test_feed_is_bounded(self):
        from face_attendance.dashboard.widgets import NotificationCenter
        center = NotificationCenter()
        for index in range(NotificationCenter.LIMIT + 5):
            center.add("checkin", f"Person {index}", "Checked in")
        self.assertEqual(len(center), NotificationCenter.LIMIT)
        self.assertEqual(center.items()[0].title, f"Person {NotificationCenter.LIMIT + 4}")

    def test_relative_time(self):
        from face_attendance.dashboard.widgets.notifications import relative_time
        now = datetime(2026, 9, 30, 15, 0, 0)
        self.assertEqual(relative_time(now - timedelta(seconds=10), now), "Just now")
        self.assertEqual(relative_time(now - timedelta(minutes=5), now), "5 min ago")
        self.assertEqual(relative_time(now - timedelta(hours=3), now), "12:00")
        self.assertEqual(relative_time(now - timedelta(days=1), now), "Yesterday 15:00")
        self.assertEqual(relative_time(now - timedelta(days=9), now), "21 Sep")


class HeaderTests(_TempAccounts, unittest.TestCase):
    def _window(self, current_user=None):
        from face_attendance.dashboard.app import MainWindow
        window = MainWindow(self.settings, use_lock=False, current_user=current_user)
        window.show()
        _app().processEvents()
        self.addCleanup(lambda: (window.close(), _app().processEvents()))
        return window

    def test_user_chip_shows_the_signed_in_account(self):
        window = self._window(self.admin)
        chip = window.user_chip
        self.assertEqual(chip.name_label.text(), "Sokha Chan")
        self.assertEqual(chip.role_label.text(), "Administrator")
        # The chip sizes to its content instead of squashing the avatar and name.
        self.assertGreaterEqual(chip.width(), chip.layout().sizeHint().width())
        window._toggle_user_menu()
        menu = window.user_menu
        self.assertTrue(menu.isVisible())
        self.assertTrue(chip.property("open"))
        self.assertEqual(menu.meta_label.text(), "sokha@example.com")
        self.assertTrue(menu.profile_item.isVisible())
        self.assertTrue(menu.sign_out_item.isVisible())
        menu.users_item.click()
        self.assertTrue(wait_until(lambda: window.page_title.text() == "Users & Access"))
        self.assertFalse(menu.isVisible())
        self.assertFalse(chip.property("open"))

    def test_local_access_hides_profile_and_sign_out(self):
        window = self._window(None)
        self.assertEqual(window.user_chip.name_label.text(), "Administrator")
        self.assertEqual(window.user_chip.role_label.text(), "Local access")
        window._toggle_user_menu()
        self.assertFalse(window.user_menu.profile_item.isVisible())
        self.assertFalse(window.user_menu.sign_out_item.isVisible())
        window.user_menu.close()

    def test_engine_events_become_notifications_with_a_badge(self):
        from face_attendance.models import CaptureJob, SaveResult
        window = self._window(self.admin)
        frame = np.full((120, 100, 3), 90, np.uint8)
        job = CaptureJob("evt-1", 1, "E-101", "Sokha Chan", time.time(), 3.2, frame)
        window.engine.attendanceSaved.emit(SaveResult(job, "saved", recorded_at=time.time()))
        window.engine.unknownFaceAlert.emit({"track_id": 7, "age": 4.0, "frame": None})
        window.engine.attendanceFailed.emit(SaveResult(job, "error", error="database is locked"))
        center = window.notifications
        self.assertEqual([item.kind for item in center.items()], ["failed", "unknown", "checkin"])
        checkin = center.items()[-1]
        self.assertEqual(checkin.body, "Checked in • ID E-101 • 3.2s verified")
        self.assertIsNotNone(checkin.thumbnail)
        self.assertEqual(window.notif_button.count(), 3)
        self.assertTrue(window.notif_button.badge.isVisible())

        window._toggle_notifications()
        panel = window.notification_panel
        self.assertTrue(panel.isVisible())
        # Nothing built for the list may steal activation and close the dropdown.
        wait_until(lambda: False, timeout=0.3)
        self.assertTrue(panel.isVisible())
        self.assertTrue(window.notif_button.property("open"))
        self.assertEqual(window.notif_button.count(), 0)
        self.assertEqual(len(panel.rows()), 3)
        self.assertGreater(panel.scroll.height(), 150)
        self.assertEqual(panel.count_pill.text(), "3 new")
        panel.set_filter("alerts")
        self.assertEqual(len(panel.rows()), 2)
        panel.close()
        self.assertEqual(center.unread_count(), 0)
        self.assertFalse(window.notif_button.property("open"))

    def test_opening_a_check_in_notification_shows_the_capture(self):
        from face_attendance.models import CaptureJob, SaveResult
        window = self._window(self.admin)
        frame = np.full((120, 100, 3), 200, np.uint8)
        job = CaptureJob("evt-2", 1, "E-101", "Sokha Chan", time.time(), 3.0, frame)
        window.engine.attendanceSaved.emit(SaveResult(job, "saved", recorded_at=time.time()))
        item = window.notifications.items()[0]
        window._toggle_notifications()
        window.notification_panel.rows()[0].activated.emit(item)
        live = window.screens[0]
        self.assertIsNotNone(live._activity_dialog)
        self.assertEqual(live._activity_dialog.entry["name"], "Sokha Chan")
        self.assertEqual(live._activity_dialog.entry["status"], "Check-in saved")
        self.assertFalse(live._activity_dialog.entry["capture"].isNull())
        live._activity_dialog.reject()
        _app().processEvents()


class UserDialogTests(unittest.TestCase):
    def setUp(self):
        _app()

    def _user(self, **changes):
        from face_attendance.users import User
        values = dict(uuid="u1", username="sokha", full_name="Sokha Chan", phone="",
                      email="sokha@example.com", password_hash="x", role="user",
                      permissions=["view_dashboard"])
        values.update(changes)
        return User(**values)

    def test_every_invalid_field_is_flagged_before_saving(self):
        from face_attendance.dashboard.screens.users import UserDialog
        dialog = UserDialog(existing_usernames=["Sokha"])
        dialog.username.setText("sokha")
        dialog.email.setText("not-an-email")
        dialog.password.setText("abc")
        dialog.confirm.setText("abd")
        dialog._validate_and_accept()
        self.assertEqual(dialog.result(), 0)
        for field, message in ((dialog.username_field, "That username is already taken."),
                               (dialog.email_field, "Enter a valid email address."),
                               (dialog.password_field, "Use at least 6 characters."),
                               (dialog.confirm_field, "Passwords don't match.")):
            self.assertTrue(field.has_error)
            self.assertEqual(field.help_label.text(), message)
        self.assertEqual(dialog.error_label.text(), "Please fix 4 highlighted fields.")

    def test_role_cards_apply_default_permissions(self):
        from face_attendance.dashboard.screens.users import UserDialog
        from face_attendance.users import ALL_PERMISSIONS, ROLE_DEFAULTS
        dialog = UserDialog()
        self.assertTrue(dialog.role_cards["user"].isChecked())
        self.assertEqual(dialog._granted(), ROLE_DEFAULTS["user"])
        dialog.role_cards["admin"].clicked.emit()
        self.assertTrue(dialog.role_cards["admin"].isChecked())
        self.assertFalse(dialog.role_cards["user"].isChecked())
        self.assertEqual(dialog._granted(), list(ALL_PERMISSIONS))
        self.assertEqual(dialog.preview_count.text(), f"{len(ALL_PERMISSIONS)} of {len(ALL_PERMISSIONS)} permissions")
        dialog.perm_tiles["manage_users"].clicked.emit()
        self.assertNotIn("manage_users", dialog._granted())
        dialog.reset_button.click()
        self.assertIn("manage_users", dialog._granted())

    def test_first_account_must_be_an_administrator(self):
        from face_attendance.dashboard.screens.users import UserDialog
        dialog = UserDialog(first_user=True)
        self.assertTrue(dialog.role_cards["admin"].isChecked())
        self.assertFalse(dialog.role_cards["user"].isEnabled())

    def test_editing_keeps_the_password_and_existing_details(self):
        from face_attendance.dashboard.screens.users import UserDialog
        dialog = UserDialog(user=self._user(email="legacy-address"), existing_usernames=["sokha", "dara"])
        dialog.phone.setText("+855 12 000 111")
        dialog._validate_and_accept()
        self.assertEqual(dialog.result(), 1)
        data = dialog.result_data()
        self.assertEqual(data["username"], "sokha")
        self.assertEqual(data["email"], "legacy-address")
        self.assertEqual(data["password"], "")
        self.assertEqual(data["permissions"], ["view_dashboard"])
        self.assertEqual(data["avatar_path"], "")

    def test_password_strength_and_match_feedback(self):
        from face_attendance.dashboard.screens.users import UserDialog
        from face_attendance.dashboard.widgets.form import password_strength
        self.assertEqual(password_strength("abc"), (0, "Too short"))
        self.assertEqual(password_strength("abcdef")[1], "Weak")
        self.assertEqual(password_strength("Abcdef12!xyz")[1], "Strong")
        dialog = UserDialog()
        dialog.password.setText("Abcdef12!xyz")
        dialog.confirm.setText("Abcdef12!xyz")
        self.assertEqual(dialog.strength.label.text(), "Strong")
        self.assertEqual(dialog.confirm_field.help_label.text(), "Passwords match")

    def test_chosen_photo_is_stored_as_a_square_copy(self):
        from PyQt6.QtGui import QColor, QImage
        from face_attendance.dashboard.screens import users
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(users, "AVATAR_DIR", Path(directory) / "avatars"):
            source = Path(directory) / "photo.png"
            image = QImage(400, 200, QImage.Format.Format_RGB32)
            image.fill(QColor("#2563EB"))
            image.save(str(source))
            stored = users.store_avatar(source)
            self.assertEqual(Path(stored).parent, Path(directory) / "avatars")
            copy = QImage(stored)
            self.assertEqual((copy.width(), copy.height()), (users.AVATAR_SIZE, users.AVATAR_SIZE))
            # Only files inside the avatar folder are ever removed.
            users.discard_avatar(str(source))
            self.assertTrue(source.exists())
            users.discard_avatar(stored)
            self.assertFalse(Path(stored).exists())

    def test_avatar_prefers_the_photo_and_has_a_neutral_placeholder(self):
        from PyQt6.QtGui import QColor, QPixmap
        from face_attendance.dashboard.widgets import Avatar
        photo = QPixmap(20, 20)
        photo.fill(QColor("#16A34A"))
        avatar = Avatar("Sokha Chan", size=40, image=photo)
        self.assertTrue(avatar.has_image())
        avatar.set_image("/does/not/exist.png")
        self.assertFalse(avatar.has_image())
        self.assertTrue(Avatar("?", size=40)._placeholder())
        self.assertFalse(Avatar("Dara", size=40)._placeholder())


class UsersScreenTests(_TempAccounts, unittest.TestCase):
    def _screen(self, current_user=None):
        from face_attendance.dashboard.screens.users import UsersScreen
        screen = UsersScreen(None, self.settings, current_user=current_user)
        screen.resize(1100, 700)
        screen.show()
        self.addCleanup(lambda: (screen.close(), _app().processEvents()))
        return screen

    def test_lists_accounts_filters_and_protects_the_signed_in_user(self):
        from PyQt6.QtWidgets import QPushButton
        self.repo.create_user("dara", "Dara Kim", "", "dara@example.com", "secret1", "user")
        screen = self._screen(self.admin)
        self.assertTrue(wait_until(lambda: len(screen._users) == 2))
        self.assertIs(screen._stack.currentWidget(), screen._table)
        self.assertEqual(screen._table.rowCount(), 2)
        self.assertEqual(screen._st_admins.value_label.text(), "1")
        for row in range(2):
            uid = screen._table.item(row, 0).data(0x0100)
            edit, toggle, delete = screen._table.cellWidget(row, 5).findChildren(QPushButton)
            self.assertTrue(edit.isEnabled())
            self.assertEqual(toggle.isEnabled(), uid != self.admin.uuid)
            self.assertEqual(delete.isEnabled(), uid != self.admin.uuid)
        screen._set_filter("admin")
        self.assertEqual(screen._table.rowCount(), 1)
        screen._set_filter("all")
        screen._search.setText("dara")
        self.assertEqual(screen._table.rowCount(), 1)
        screen._search.setText("nobody")
        self.assertIs(screen._stack.currentWidget(), screen._no_results)
        screen._clear_filters()
        self.assertEqual(screen._table.rowCount(), 2)

    def test_empty_database_prompts_for_the_first_administrator(self):
        self.repo.delete_user(self.admin.uuid)
        screen = self._screen()
        self.assertTrue(wait_until(lambda: screen._stack.currentWidget() is screen._empty))

    def test_load_failure_offers_a_retry_instead_of_claiming_no_accounts(self):
        from face_attendance.users import UserRepository
        with patch.object(UserRepository, "list_users", side_effect=RuntimeError("server gone")):
            screen = self._screen()
            self.assertTrue(wait_until(lambda: screen._stack.currentWidget() is screen._load_error))
        self.assertIn("server gone", screen.toast.label.text())
        screen._refresh()
        self.assertTrue(wait_until(lambda: screen._stack.currentWidget() is screen._table))


class StrayWindowTests(unittest.TestCase):
    """Showing a widget before it has a parent briefly makes it a top-level window,
    which steals activation (closing open dropdowns) and can flash on screen."""

    def _windows_shown_while(self, build):
        from PyQt6.QtCore import QEvent, QObject
        from PyQt6.QtWidgets import QWidget

        class Spy(QObject):
            def __init__(self):
                super().__init__()
                self.shown = []

            def eventFilter(self, watched, event):
                if (event.type() == QEvent.Type.Show and isinstance(watched, QWidget)
                        and watched.isWindow()):
                    self.shown.append(type(watched).__name__)
                return False

        application = _app()
        spy = Spy()
        application.installEventFilter(spy)
        try:
            built = build()
        finally:
            application.removeEventFilter(spy)
        return spy.shown, built

    def test_building_forms_rows_and_cards_opens_no_windows(self):
        from face_attendance.dashboard.login import LoginDialog, UserLoginDialog
        from face_attendance.dashboard.screens.users import UserDialog
        from face_attendance.dashboard.widgets import Card, NotificationCenter, PageHeader
        from face_attendance.dashboard.widgets.form import FormField, SectionHeading, TextInput
        from face_attendance.dashboard.widgets.notifications import NotificationRow
        from face_attendance.settings import Settings
        item = NotificationCenter().add("checkin", "Sokha", "Checked in")
        shown, _built = self._windows_shown_while(lambda: [
            FormField("Username", TextInput(), help_text="At least 3 characters."),
            SectionHeading("Credentials", "A hint"),
            NotificationRow(item),
            Card("Title", "Subtitle"),
            PageHeader("Title", "Subtitle"),
            UserDialog(),
            UserLoginDialog(Settings()),
            LoginDialog("x"),
        ])
        self.assertEqual(shown, [])


class ThemeTests(unittest.TestCase):
    def test_checkbox_indicators_have_a_tick_image(self):
        from face_attendance.dashboard.theme import _indicator_images, stylesheet
        _app()
        images = _indicator_images()
        self.assertTrue(Path(images["checked"]).is_file())
        self.assertTrue(Path(images["checked"].replace(".png", "@2x.png")).is_file())
        self.assertIn(images["checked"], stylesheet("light"))
        self.assertIn(images["indeterminate"], stylesheet("dark"))


if __name__ == "__main__":
    unittest.main()
