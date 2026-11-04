"""Role permissions in the dashboard shell, and disabled accounts.

Navigation the account cannot open is hidden; any other way in, and guarded
actions inside screens, show an "Access denied" notice.  Disabled accounts
cannot sign in and are signed out when disabled elsewhere.
"""
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.test_dashboard import _app, temp_settings
from tests.test_dashboard_access import _TempAccounts, wait_until

STANDARD = ["view_dashboard", "view_reports"]


def _accept_destructive(box):
    """Stand-in for ``QMessageBox.clickedButton``: the destructive choice."""
    from PyQt6.QtWidgets import QMessageBox
    return next(button for button in box.buttons()
                if box.buttonRole(button) == QMessageBox.ButtonRole.DestructiveRole)


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        _app()

    def _user(self, permissions):
        from face_attendance.users import User
        return User(uuid="u", username="dara", full_name="Dara Kim", phone="", email="",
                    password_hash="x", role="user", permissions=list(permissions))

    def test_no_account_means_full_access(self):
        from face_attendance.dashboard.access import AccessControl
        access = AccessControl()
        self.assertFalse(access.restricted())
        self.assertTrue(access.allows("manage_users"))
        self.assertTrue(access.allows_nav("Settings"))

    def test_permissions_decide_navigation_and_actions(self):
        from face_attendance.dashboard.access import AccessControl, NAV_PERMISSIONS
        from face_attendance.dashboard.app import NAV
        access = AccessControl(self._user(STANDARD))
        self.assertEqual(set(NAV_PERMISSIONS), {title for _icon, title in NAV})
        self.assertEqual([title for _icon, title in NAV if access.allows_nav(title)],
                         ["Live Monitor", "Attendance Report", "Live Usage", "License Plates"])
        self.assertFalse(access.allows("control_engine"))
        self.assertEqual(access.account_label(), "Signed in as Dara Kim • Standard user")

    def test_guard_without_a_window_policy_allows(self):
        from PyQt6.QtWidgets import QWidget
        from face_attendance.dashboard.access import guard
        self.assertTrue(guard(QWidget(), "manage_settings", "open Settings"))

    def test_notice_names_the_action_and_permission(self):
        from face_attendance.dashboard.widgets.access_denied import AccessDeniedDialog
        notice = AccessDeniedDialog("open Settings", "Manage settings", "Signed in as Dara Kim")
        self.assertEqual(notice.message.text(), "You don't have permission to open Settings.")
        self.assertEqual(notice.requirement.text(), "Requires “Manage settings”")
        self.assertEqual(notice.account.text(), "Signed in as Dara Kim")


class RestrictedWindowTests(_TempAccounts, unittest.TestCase):
    def _window(self, permissions, role="user"):
        from face_attendance.dashboard.app import MainWindow
        user = self.repo.create_user("dara", "Dara Kim", "", "", "secret1", role,
                                     permissions=list(permissions))
        window = MainWindow(self.settings, use_lock=False, current_user=user)
        window.show()
        _app().processEvents()

        def close():
            notice = window.access.notice()
            if notice is not None:
                notice.reject()
            window.close()
            _app().processEvents()
        self.addCleanup(close)
        return window

    def _visible_nav(self, window):
        return [window.nav.item(row).text() for row in range(window.nav.count())
                if not window.nav.item(row).isHidden()]

    def _dismiss(self, window):
        notice = window.access.notice()
        self.assertIsNotNone(notice, "expected an access-denied notice")
        text = notice.message.text()
        notice.reject()
        _app().processEvents()
        return text

    def test_menus_show_only_permitted_areas(self):
        window = self._window(STANDARD)
        self.assertEqual(self._visible_nav(window),
                         ["Live Monitor", "Attendance Report", "Live Usage", "License Plates"])
        self.assertEqual(window.page_title.text(), "Live Monitor")
        self.assertTrue(window.user_menu.settings_item.isHidden())
        self.assertFalse(window.notif_button.isHidden())
        live = window.screens[0]
        self.assertFalse(live.view_all_button.isHidden())
        self.assertTrue(live.preview_hud.settings.isHidden())
        self.assertFalse(live.camera_settings_action.isVisible())

    def test_other_ways_into_a_hidden_screen_are_refused(self):
        window = self._window(STANDARD)
        window._go("Settings")
        self.assertEqual(self._dismiss(window), "You don't have permission to open Settings.")
        self.assertEqual(window.page_title.text(), "Live Monitor")
        window.nav.setCurrentRow(4)
        self.assertEqual(self._dismiss(window), "You don't have permission to open Settings.")
        self.assertEqual(window.nav.currentRow(), 0)
        self.assertIs(window.stack.currentWidget(), window.screens[0])
        window.screens[0].settingsRequested.emit()
        self._dismiss(window)
        self.assertEqual(window.page_title.text(), "Live Monitor")

    def test_guarded_actions_explain_instead_of_running(self):
        window = self._window(STANDARD)
        live = window.screens[0]
        live.preview_hud.record.click()
        self.assertEqual(self._dismiss(window), "You don't have permission to start or stop the engine.")
        self.assertFalse(window.engine.running)
        live._restart()
        self.assertEqual(self._dismiss(window), "You don't have permission to restart the engine.")
        live._apply_requirements()
        self.assertIn("attendance requirements", self._dismiss(window))
        report = window.screens[1]
        report._export("all")
        self.assertEqual(self._dismiss(window), "You don't have permission to export attendance records.")
        report._delete_selected()
        self.assertEqual(self._dismiss(window), "You don't have permission to delete attendance records.")

    def test_only_one_notice_at_a_time(self):
        window = self._window(STANDARD)
        live = window.screens[0]
        first = window.access.deny("control_engine", "restart the engine", live)
        second = window.access.deny("control_engine", "restart the engine", live)
        self.assertIs(first, second)

    def test_an_account_without_permissions_sees_the_no_access_page(self):
        window = self._window([])
        self.assertEqual(self._visible_nav(window), [])
        self.assertIs(window.stack.currentWidget(), window.no_access_page)
        self.assertEqual(window.page_title.text(), "No access")
        self.assertTrue(window.notif_button.isHidden())

    def test_permission_changes_apply_without_restarting(self):
        window = self._window(STANDARD)
        window._go("Attendance Report")
        promoted = self.repo.get_user(window.current_user.uuid)
        promoted.permissions = STANDARD + ["manage_settings"]
        window._apply_account(promoted)
        self.assertIn("Settings", self._visible_nav(window))
        self.assertFalse(window.user_menu.settings_item.isHidden())
        # Losing the screen you are on moves you to the first one still allowed.
        demoted = self.repo.get_user(window.current_user.uuid)
        demoted.permissions = ["view_dashboard"]
        window._apply_account(demoted)
        self.assertEqual(self._visible_nav(window), ["Live Monitor", "Live Usage", "License Plates"])
        self.assertEqual(window.page_title.text(), "Live Monitor")

    def test_disabling_the_signed_in_account_signs_it_out(self):
        from PyQt6.QtWidgets import QMessageBox
        from face_attendance import users
        from face_attendance.dashboard.app import MainWindow
        window = self._window(STANDARD)
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / ".session"
            session.write_text("{}", encoding="utf-8")
            with patch.object(users, "SESSION_PATH", session), \
                    patch.object(MainWindow, "_relaunch_app") as relaunch:
                disabled = self.repo.get_user(window.current_user.uuid)
                disabled.disabled = True
                window._apply_account(disabled)
                self.assertFalse(session.exists())
                box = next(widget for widget in window.findChildren(QMessageBox)
                           if widget.isVisible())
                self.assertEqual(box.text(), "Your account was disabled by an administrator.")
                box.accept()
                self.assertTrue(wait_until(lambda: relaunch.called))


class DisabledAccountTests(_TempAccounts, unittest.TestCase):
    def test_disabled_flag_round_trips_and_blocks_sign_in(self):
        from face_attendance.users import AccountDisabledError
        dara = self.repo.create_user("dara", "Dara Kim", "", "", "secret1", "user")
        self.assertFalse(dara.disabled)
        self.repo.set_disabled(dara.uuid, True)
        self.assertTrue(self.repo.get_user(dara.uuid).disabled)
        with self.assertRaises(AccountDisabledError):
            self.repo.authenticate("dara", "secret1")
        # A wrong password on a disabled account reveals nothing more.
        self.assertIsNone(self.repo.authenticate("dara", "nope"))
        self.repo.set_disabled(dara.uuid, False)
        self.assertEqual(self.repo.authenticate("dara", "secret1").uuid, dara.uuid)

    def test_existing_tables_gain_the_disabled_column(self):
        from face_attendance.users import UserRepository
        with tempfile.TemporaryDirectory() as directory:
            settings = temp_settings(directory)
            with sqlite3.connect(settings.db_path) as conn:
                conn.execute("""CREATE TABLE users (uuid TEXT PRIMARY KEY, username TEXT NOT NULL,
                    full_name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user', permissions TEXT NOT NULL DEFAULT '[]',
                    avatar_path TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL)""")
                conn.execute("INSERT INTO users (uuid, username, password_hash, created_at, updated_at) "
                             "VALUES ('old', 'legacy', 'x', 'now', 'now')")
            repo = UserRepository(settings)
            legacy = repo.get_user("old")
            self.assertFalse(legacy.disabled)
            repo.set_disabled("old", True)
            self.assertTrue(repo.get_user("old").disabled)

    def test_sign_in_explains_a_disabled_account_without_using_an_attempt(self):
        from face_attendance.dashboard.login import DISABLED_MESSAGE, UserLoginDialog
        dara = self.repo.create_user("dara", "Dara Kim", "", "", "secret1", "user")
        self.repo.set_disabled(dara.uuid, True)
        with patch("face_attendance.dashboard.login.STAGE_MIN_MS", 1):
            dialog = UserLoginDialog(self.settings)
            dialog.show()
            dialog.username.setText("dara")
            dialog.password.setText("secret1")
            dialog._submit()
            self.assertTrue(wait_until(lambda: not dialog.is_busy()))
        self.assertEqual(dialog.alert.text(), DISABLED_MESSAGE)
        self.assertEqual(dialog.attempts, 0)
        self.assertIsNone(dialog.current_user)
        dialog.close()

    def test_sign_in_can_open_with_a_notice(self):
        from face_attendance.dashboard.login import UserLoginDialog
        dialog = UserLoginDialog(self.settings, notice="Your account has been disabled.")
        dialog.show()
        self.assertEqual(dialog.alert.text(), "Your account has been disabled.")
        dialog.close()

    def test_watcher_reports_changes_made_elsewhere(self):
        from face_attendance.dashboard.access import AccountWatcher
        seen = []
        watcher = AccountWatcher(self.settings, self.admin)
        watcher.changed.connect(seen.append)
        with patch("face_attendance.dashboard.access.hrm_auth.check_account") as check:
            check.return_value = self.admin
            watcher.check_now()
            wait_until(lambda: False, timeout=0.3)
            self.assertEqual(seen, [], "an unchanged account emits nothing")
            check.return_value = replace(self.admin, disabled=True)
            watcher.check_now()
            self.assertTrue(wait_until(lambda: seen))
        self.assertTrue(seen[0].disabled)
        watcher.stop()


if __name__ == "__main__":
    unittest.main()
