"""A released app must be able to open (or create) the user database and sign in."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from face_attendance.settings import Settings
from face_attendance.users import (DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, UserRepository,
                                   account_login_required, ensure_default_admin)

ROOT = Path(__file__).resolve().parent.parent


class ReleaseDatabaseTests(unittest.TestCase):
    def test_fresh_install_creates_the_database_and_allows_first_sign_in(self):
        with tempfile.TemporaryDirectory() as directory:
            # The data folder of a fresh install does not exist yet.
            settings = Settings(db_path=str(Path(directory) / "SV Face ID" / "attendance.db"))
            repository = UserRepository(settings)
            self.assertFalse(repository.has_users())
            self.assertTrue(Path(settings.db_path).is_file())
            repository.create_user("admin", "Admin", "", "", "correct horse", role="admin")
            self.assertEqual(repository.authenticate("admin", "correct horse").username, "admin")

    def test_release_creates_default_admin_once_when_no_accounts_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = UserRepository(Settings(db_path=str(Path(directory) / "attendance.db")))
            self.assertEqual((DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD), ("timdev", "00000000"))
            self.assertEqual(ensure_default_admin(repository, frozen=True).role, "admin")
            user = repository.authenticate("timdev", "00000000")
            self.assertEqual(user.role, "admin")
            self.assertIsNone(ensure_default_admin(repository, frozen=True))
            self.assertEqual(len(repository.list_users()), 1)

    def test_default_admin_never_replaces_existing_accounts_or_runs_from_source(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = UserRepository(Settings(db_path=str(Path(directory) / "attendance.db")))
            self.assertIsNone(ensure_default_admin(repository, frozen=False))
            self.assertFalse(repository.has_users())
            repository.create_user("owner", "Owner", "", "", "correct horse", role="admin")
            self.assertIsNone(ensure_default_admin(repository, frozen=True))
            self.assertIsNone(repository.authenticate("timdev", "00000000"))

    def test_database_outage_creates_nothing_and_still_requires_sign_in(self):
        repository = Mock()
        repository.has_users.side_effect = OSError("unreachable database")
        with self.assertLogs(level="ERROR"):
            self.assertIsNone(ensure_default_admin(repository, frozen=True))
        repository.create_user.assert_not_called()
        with self.assertLogs(level="ERROR"):
            self.assertTrue(account_login_required(repository))

    def test_release_builds_install_and_bundle_remote_database_drivers(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for package in ("psycopg2-binary", "mysql-connector-python"):
            self.assertRegex(requirements, rf"(?m)^{re.escape(package)}==")
        spec = (ROOT / "build.spec").read_text(encoding="utf-8")
        for module in ("psycopg2", "mysql.connector"):
            self.assertIn(f'"{module}"', spec)


if __name__ == "__main__":
    unittest.main()
