"""Release builds install only requirements.txt, so a package that merely happens to be
in a developer's environment works on their computer and is missing everywhere else."""
import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIRST_PARTY = {"face_attendance", "license_plate"}
# Import name -> the name it is installed under, where they differ.
INSTALLED_AS = {"cv2": "opencv-python", "psycopg2": "psycopg2-binary", "mysql": "mysql-connector-python"}
# Separate extras, each with its own requirements file and a message when absent.
OPTIONAL = {
    "anyio", "cryptography", "fastapi", "pydantic", "starlette", "uniface",  # requirements-mobile.txt
    "fast_alpr",                                                              # requirements-plates.txt
    "simpleaudio",                                                            # requirements-audio.txt
}


def imported_packages():
    found = set()
    for path in [ROOT / "dashboard.py", *(ROOT / "face_attendance").rglob("*.py"),
                 *(ROOT / "license_plate").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found - set(sys.stdlib_module_names) - FIRST_PARTY


def required_packages():
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        match = re.match(r"[A-Za-z0-9_.-]+", line.strip())
        if match:
            names.add(match.group().lower())
    return names


class ReleaseRequirementsTests(unittest.TestCase):
    def test_every_imported_package_is_a_release_requirement(self):
        required = required_packages()
        missing = {name for name in imported_packages() - OPTIONAL
                   if INSTALLED_AS.get(name, name).lower() not in required}
        self.assertEqual(missing, set())

    def test_telegram_and_excel_are_bundled(self):
        spec = (ROOT / "build.spec").read_text(encoding="utf-8")
        for name in ("requests", "openpyxl"):
            self.assertIn(f'"{name}"', spec)
            self.assertIn(name, required_packages())


if __name__ == "__main__":
    unittest.main()
