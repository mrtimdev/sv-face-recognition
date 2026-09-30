"""Stamp the application version, or print it.

    python scripts/set_version.py v1.0.6    # writes face_attendance/version.py, prints 1.0.6
    python scripts/set_version.py           # prints the current version

CI runs the first form with the pushed tag before building, so the app, the
macOS Info.plist and the Windows installer name all carry the tag's version.
"""
import re
import sys
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "face_attendance" / "version.py"
PATTERN = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
SEMVER = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def current():
    match = PATTERN.search(VERSION_FILE.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"No __version__ line in {VERSION_FILE}")
    return match.group(1)


def stamp(tag):
    match = SEMVER.match(tag.strip())
    if not match:
        raise SystemExit(f"Version tags must look like v1.2.3, got {tag!r}")
    version = match.group(1)
    text = VERSION_FILE.read_text(encoding="utf-8")
    VERSION_FILE.write_text(PATTERN.sub(f'__version__ = "{version}"', text), encoding="utf-8")
    return version


if __name__ == "__main__":
    print(stamp(sys.argv[1]) if len(sys.argv) > 1 else current())
