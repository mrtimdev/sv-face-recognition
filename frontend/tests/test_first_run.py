"""First-run setup: when it appears, MySQL provisioning, the wizard and recovery."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import mysql.connector
from PyQt6.QtWidgets import QApplication

from face_attendance import first_run
from face_attendance.dashboard.setup_wizard import ADMIN, DATABASE, FINISH, MYSQL, SetupWizard
from face_attendance.settings import Settings
from face_attendance.users import (DEFAULT_ADMIN_PASSWORD, UserRepository,
                                   verify_password)


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((sql, params))


class FakeConnection:
    def __init__(self):
        self.log, self.closed = [], False

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        pass

    def close(self):
        self.closed = True


class TemporarySettings(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings_path = self.root / "settings.json"
        self.settings = Settings(db_path=str(self.root / "attendance.db"))
        self.settings._path = self.settings_path


class _Qt(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class NeedsSetupTests(TemporarySettings):
    def needs(self, frozen=True):
        return first_run.needs_setup(self.settings_path, frozen=frozen, root=self.root)

    def test_only_a_fresh_release_asks(self):
        self.assertTrue(self.needs())
        self.assertFalse(self.needs(frozen=False))

    def test_empty_database_from_the_splash_check_is_still_a_first_run(self):
        (self.root / "attendance.db").touch()
        self.assertTrue(self.needs())
        UserRepository(self.settings).has_users()  # tables but no rows
        self.assertTrue(self.needs())

    def test_existing_install_is_never_asked_again(self):
        UserRepository(self.settings).create_user("old", "Old", "", "", "old secret", role="admin")
        self.assertFalse(self.needs())

    def test_saved_settings_or_unreadable_database_are_not_a_first_run(self):
        (self.root / "attendance.db").write_bytes(b"not a database, but maybe someone's data")
        self.assertFalse(self.needs())
        (self.root / "attendance.db").unlink()
        self.settings_path.write_text("{}")
        self.assertFalse(self.needs())


class ExistingInstallTests(TemporarySettings, _Qt):
    def admin(self):
        return UserRepository(self.settings).create_user("old", "", "", "", "old secret", role="admin")

    def offer(self, user, has_accounts=True, forced=False):
        return first_run.offer_after_sign_in(user, has_accounts, forced=forced, frozen=True, root=self.root)

    def test_shown_once_after_an_administrator_signs_in(self):
        admin = self.admin()
        self.settings_path.write_text("{}")
        self.assertFalse(first_run.needs_setup(self.settings_path, frozen=True, root=self.root))
        self.assertTrue(self.offer(admin))
        first_run.mark_setup_complete(self.root)
        self.assertFalse(self.offer(admin))
        self.assertTrue(self.offer(admin, forced=True))

    def test_never_shown_without_permission_or_before_sign_in(self):
        user = UserRepository(self.settings).create_user("staff", "", "", "", "staff secret", role="user")
        self.assertFalse(self.offer(user))
        self.assertFalse(self.offer(None))
        self.assertFalse(self.offer(None, forced=True))  # --setup can't skip sign-in either.

    def test_source_runs_are_not_prompted_and_marker_stops_a_new_install_prompt(self):
        self.assertFalse(first_run.offer_after_sign_in(self.admin(), True, frozen=False, root=self.root))
        first_run.mark_setup_complete(self.root)
        self.assertFalse(first_run.needs_setup(self.settings_path, frozen=True, root=self.root))

    def test_keeping_the_current_database_changes_nothing(self):
        self.admin()
        before = self.settings.to_dict()
        wizard = SetupWizard(self.settings, existing=True)
        self.addCleanup(wizard.deleteLater)
        wizard._next()
        wizard._next()
        wizard._next()
        self.assertEqual(wizard.pages.currentIndex(), FINISH)
        self.assertEqual(self.settings.to_dict(), before)
        self.assertEqual(len(UserRepository(self.settings).list_users()), 1)
        self.assertIn("unchanged", wizard.summary.text())


class ProvisionTests(TemporarySettings):
    def provision(self, connection=None, **kwargs):
        connection = connection or FakeConnection()
        with patch("mysql.connector.connect", return_value=connection) as connect, \
                patch.object(first_run, "AttendanceRepository"), \
                patch.object(first_run, "UserRepository") as users:
            users.return_value.has_users.return_value = False
            result = first_run.provision_local_mysql(self.settings, "root", "admin secret", **kwargs)
        return result, connection, connect

    def test_creates_database_and_private_user_without_keeping_admin_password(self):
        (values, has_accounts), connection, connect = self.provision()
        self.assertFalse(has_accounts)
        statements = " ".join(sql for sql, _ in connection.log)
        self.assertIn("CREATE DATABASE IF NOT EXISTS `sv_attendance`", statements)
        hosts = {params[1] for sql, params in connection.log if sql.startswith("CREATE USER")}
        self.assertEqual(hosts, {"localhost", "127.0.0.1"})
        self.assertTrue(connection.closed)
        self.assertTrue(connect.call_args.kwargs["use_pure"])
        self.assertEqual((values["db_backend"], values["db_host"], values["db_user"]),
                         ("mysql", "127.0.0.1", "sv_face"))
        self.assertNotIn("admin secret", values.values())
        self.assertEqual(self.settings.db_backend, "sqlite")  # Nothing changes until the wizard saves.

    def test_unsafe_database_names_never_reach_mysql(self):
        for name in ("x`; DROP DATABASE y", "", "has space", "a" * 65):
            with self.assertRaises(first_run.SetupError):
                self.provision(database=name)

    def test_connection_failures_are_explained(self):
        for errno, expected in ((1045, "rejected"), (2003, "not running")):
            with patch("mysql.connector.connect", side_effect=mysql.connector.Error("x", errno=errno)):
                with self.assertRaisesRegex(first_run.SetupError, expected):
                    first_run.provision_local_mysql(self.settings, "root", "bad")


class RecoveryTests(TemporarySettings):
    def setUp(self):
        super().setUp()
        self.repository = UserRepository(self.settings)

    def test_broken_hashing_is_an_error_not_a_wrong_password(self):
        stored = self.repository.create_user("a", "", "", "", "secret value").password_hash
        with patch("hashlib.pbkdf2_hmac", side_effect=AttributeError("no OpenSSL")):
            with self.assertRaises(AttributeError):
                verify_password("secret value", stored)


class WizardTests(TemporarySettings):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wizard(self):
        wizard = SetupWizard(self.settings)
        self.addCleanup(wizard.deleteLater)
        return wizard

    def test_built_in_database_path_skips_mysql_and_saves_at_the_end(self):
        wizard = self.wizard()
        wizard._next()
        self.assertEqual(wizard.pages.currentIndex(), DATABASE)
        wizard._next()
        self.assertEqual(wizard.pages.currentIndex(), ADMIN)
        self.assertFalse(self.settings_path.exists())
        wizard.hrm_address.setText("invalid")
        wizard._next()
        self.assertEqual(wizard.pages.currentIndex(), ADMIN)
        wizard.hrm_address.setText("https://hrm.example.com")
        wizard._next()
        self.assertEqual(wizard.pages.currentIndex(), FINISH)
        self.assertTrue(self.settings_path.exists())
        self.assertFalse(UserRepository(self.settings).has_users())

    def test_mysql_path_applies_provisioned_settings(self):
        wizard = self.wizard()
        wizard._next()
        wizard._choose_mysql(True)
        with patch.object(first_run, "mysql_running", return_value=False):
            wizard._next()
        self.assertEqual(wizard.pages.currentIndex(), MYSQL)
        self.assertFalse(wizard.install_help.isHidden())
        wizard._on_provisioned(None, "MySQL rejected that username or password.")
        self.assertEqual(wizard.pages.currentIndex(), MYSQL)
        self.assertIn("rejected", wizard.error.text())
        wizard._on_provisioned(dict(db_backend="mysql", db_name="sv_attendance"), True)
        self.assertEqual(wizard.pages.currentIndex(), ADMIN)
        wizard._next()
        self.assertEqual(wizard.pages.currentIndex(), FINISH)
        self.assertEqual(self.settings.db_backend, "mysql")



if __name__ == "__main__":
    unittest.main()
