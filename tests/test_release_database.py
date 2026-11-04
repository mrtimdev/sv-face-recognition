"""A released app must be able to open (or create) the user database and sign in."""
import re
import tempfile
import unittest
from pathlib import Path


from face_attendance.settings import Settings
from face_attendance.users import UserRepository

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

    def test_release_builds_install_and_bundle_remote_database_drivers(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for package in ("psycopg2-binary", "mysql-connector-python"):
            self.assertRegex(requirements, rf"(?m)^{re.escape(package)}==")
        spec = (ROOT / "build.spec").read_text(encoding="utf-8")
        for module in ("psycopg2", "mysql.connector"):
            self.assertIn(f'"{module}"', spec)


if __name__ == "__main__":
    unittest.main()
