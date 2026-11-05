"""Application updates from GitHub Releases: check, download, verify, install.

``check_latest()`` reads the newest published release, ``download()`` streams
the asset for this platform and verifies it against the SHA-256 digest GitHub
publishes (nothing unverified is ever installed), and ``install_command()``
returns the process that finishes the update once this app has exited:

* Windows - the Inno Setup installer, run silently with ``/RELAUNCH=1`` so it
  reopens the app (started only after this process has quit);
* macOS - a small shell script that copies the ``.app`` bundle out of the
  downloaded disk image, then opens it.  ``install_target()`` picks the place:
  the running bundle when its folder is writable, otherwise Applications (an
  app opened straight from its disk image runs from a read-only copy).

``record_pending_install()`` notes what is being installed and the script
leaves a result, so the next start reports whether the update really
happened (``take_install_outcome()``) instead of silently reopening the old
version.  Running from source there is nothing to install over; the check
still works.
"""
import hashlib
import json
import logging
import os
import re
import shlex
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT
from .version import __version__

REPOSITORY = "mrtimdev/sv-face-recognition"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD_DIR = ROOT / "updates"
USER_AGENT = f"SV-Face-ID/{__version__}"
CHUNK_BYTES = 256 * 1024
WINDOWS_INSTALLER_ARGS = "/SILENT /SP- /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /RELAUNCH=1"
# Where a macOS update goes when the running copy can't be replaced in place.
APPLICATION_FOLDERS = (Path("/Applications"), Path.home() / "Applications")
PENDING_FILE = "pending-install.json"     # written before installing
RESULT_FILE = "install-result.txt"        # "ok", or why the installer failed

_VERSION = re.compile(r"^\s*v?(\d+)\.(\d+)\.(\d+)")


class UpdateError(Exception):
    """A check, download or install step failed; the message is shown to the user."""


class UpdateCancelled(Exception):
    """The user cancelled a download."""


def parse_version(text):
    """``"v1.2.3"`` -> ``(1, 2, 3)``; raises ``ValueError`` for anything else."""
    match = _VERSION.match(str(text or ""))
    if not match:
        raise ValueError(f"Not a version: {text!r}")
    return tuple(int(part) for part in match.groups())


def is_newer(candidate, current=__version__):
    try:
        return parse_version(candidate) > parse_version(current)
    except ValueError:
        return False


def platform_key(system=None):
    system = system or sys.platform
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    return None


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    url: str
    digest: str = ""          # "sha256:<hex>" when GitHub provides it


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    title: str
    notes: str
    published_at: str
    page_url: str
    asset: Asset = None

    def is_newer(self, current=__version__):
        return is_newer(self.version, current)


def pick_asset(assets, system=None, version=None):
    """The installer asset for this platform: a Windows setup ``.exe`` or a macOS ``.dmg``.

    When several match, one named after *version* wins, so an installer left over
    from an older build (e.g. ``...-1.0.3.exe`` on a later release) is never chosen.
    """
    key = platform_key(system)
    wanted = {"windows": ".exe", "macos": ".dmg"}.get(key)
    if wanted is None:
        return None
    matches = [item for item in assets or [] if str(item.get("name", "")).lower().endswith(wanted)]
    matches.sort(key=lambda item: (bool(version) and version not in item["name"],
                                   key == "windows" and "setup" not in item["name"].lower()))
    if not matches:
        return None
    item = matches[0]
    return Asset(name=item["name"], size=int(item.get("size") or 0),
                 url=item.get("browser_download_url", ""), digest=item.get("digest") or "")


def check_latest(timeout=10, opener=urllib.request.urlopen, system=None):
    """The newest published (non-draft, non-prerelease) release."""
    request = urllib.request.Request(LATEST_URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No published release was found.") from exc
        if exc.code in (403, 429):
            raise UpdateError("GitHub is limiting update checks right now. Try again later.") from exc
        raise UpdateError(f"GitHub answered with an error ({exc.code}).") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError("Couldn't reach GitHub. Check the internet connection and try again.") from exc
    except ValueError as exc:
        raise UpdateError("GitHub sent an unexpected response.") from exc
    tag = str(data.get("tag_name") or "")
    try:
        version = ".".join(str(part) for part in parse_version(tag))
    except ValueError as exc:
        raise UpdateError(f"The latest release tag {tag!r} is not a version number.") from exc
    return Release(version=version, tag=tag, title=str(data.get("name") or tag),
                   notes=str(data.get("body") or "").strip(),
                   published_at=str(data.get("published_at") or ""),
                   page_url=str(data.get("html_url") or ""),
                   asset=pick_asset(data.get("assets"), system, version))


def _expected_sha256(asset):
    kind, _, value = (asset.digest or "").partition(":")
    return value.lower() if kind.lower() == "sha256" and value else ""


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def download(asset, directory=None, progress=None, cancelled=None, timeout=30,
             opener=urllib.request.urlopen):
    """Stream *asset* to *directory*, verify it and return the finished file's path.

    Data goes to ``<name>.part`` and is renamed only after the size and SHA-256
    match, so an interrupted or tampered download never looks complete.  A copy
    already on disk that still verifies is reused.
    """
    directory = Path(directory or DOWNLOAD_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    name = Path(asset.name).name          # never let a release name choose the folder
    if not name or name in (".", ".."):
        raise UpdateError("The release asset has no usable file name.")
    target = directory / name
    expected = _expected_sha256(asset)
    if target.exists() and expected and _file_sha256(target) == expected:
        if progress is not None:
            progress(asset.size, asset.size)
        return target
    partial = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    received = 0
    request = urllib.request.Request(asset.url, headers={"User-Agent": USER_AGENT,
                                                         "Accept": "application/octet-stream"})
    try:
        with opener(request, timeout=timeout) as response, open(partial, "wb") as handle:
            total = int(response.headers.get("Content-Length") or asset.size or 0)
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateCancelled()
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                handle.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if progress is not None:
                    progress(received, total)
    except UpdateCancelled:
        partial.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError("The download was interrupted. Check the connection and try again.") from exc
    if asset.size and received != asset.size:
        partial.unlink(missing_ok=True)
        raise UpdateError("The download was incomplete. Nothing was installed.")
    if expected and digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise UpdateError("The download failed its checksum. Nothing was installed.")
    os.replace(partial, target)
    return target


def remove_stale_downloads(directory=None, current=__version__):
    """Delete unfinished downloads and installers not newer than the running version."""
    directory = Path(directory or DOWNLOAD_DIR)
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        stale = path.suffix == ".part"
        if not stale:
            match = re.search(r"(\d+\.\d+\.\d+)", path.name)
            stale = bool(match) and not is_newer(match.group(1), current)
        if stale:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def installed_location(executable=None, system=None, frozen=None):
    """The installed app (the ``.app`` bundle, or the Windows install folder); None from source."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return None
    path = Path(executable or sys.executable).resolve()
    key = platform_key(system)
    if key == "macos":
        return next((parent for parent in path.parents if parent.suffix == ".app"), None)
    if key == "windows":
        return path.parent
    return None


def translocated_original(path):
    """The real location of a translocated macOS app, or None when *path* isn't one.

    macOS runs a downloaded app that Finder didn't move - typically one opened
    straight from its disk image - from a random read-only copy under
    ``.../AppTranslocation/`` ("App Translocation").  Only the Security
    framework knows where the original is.
    """
    if "/AppTranslocation/" not in str(path):
        return None
    try:
        return _security_original_path(str(path))
    except Exception:     # a changed or missing framework symbol must never break the app
        logging.debug("could not resolve the translocated app", exc_info=True)
        return None


def _security_original_path(path):
    import ctypes
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
    cf.CFURLCreateFromFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                           ctypes.c_long, ctypes.c_bool]
    cf.CFURLGetFileSystemRepresentation.restype = ctypes.c_bool
    cf.CFURLGetFileSystemRepresentation.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                                    ctypes.c_char_p, ctypes.c_long]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    original_for = security.SecTranslocateCreateOriginalPathForURL
    original_for.restype = ctypes.c_void_p
    original_for.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    raw = os.fsencode(path)
    url = cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), True)
    if not url:
        return None
    try:
        original = original_for(url, None)
        if not original:
            return None
        try:
            buffer = ctypes.create_string_buffer(4096)
            if not cf.CFURLGetFileSystemRepresentation(original, True, buffer, len(buffer)):
                return None
            result = Path(os.fsdecode(buffer.value))
        finally:
            cf.CFRelease(original)
    finally:
        cf.CFRelease(url)
    return None if result == Path(path) else result


def opened_from_disk_image(location):
    """True when the running app is on a mounted disk image (directly or translocated)."""
    if location is None:
        return False
    return str(translocated_original(location) or location).startswith("/Volumes/")


def folder_label(folder):
    """A short, friendly name for an install folder."""
    folder = Path(folder)
    if folder == Path("/Applications"):
        return "Applications"
    if folder == Path.home() / "Applications":
        return "Applications in your home folder"
    return str(folder)


def _can_write(folder):
    """True when a bundle can be created in *folder*, creating the folder if needed."""
    folder = Path(folder)
    if folder.is_dir():
        return os.access(folder, os.W_OK)
    return not folder.exists() and folder.parent.is_dir() and os.access(folder.parent, os.W_OK)


def install_target(location, system=None, folders=None):
    """Where the update will be installed: ``(path, "")``, or ``(None, reason)``.

    Windows: the installer updates the existing installation.  macOS: the
    running bundle is replaced where it is when that folder is writable.  A copy
    that can't be replaced - the disk image is read-only, and so is the copy
    macOS runs a translocated app from - gets the update in Applications
    instead (``~/Applications`` when the account can't write to /Applications).
    """
    key = platform_key(system)
    if key is None:
        return None, "Automatic updates are available on Windows and macOS."
    if location is None:
        return None, ("You're running from source. Updates install automatically in the "
                      "packaged app; here, pull the latest code instead.")
    location = Path(location)
    if key == "windows":
        return location, ""
    bundle = translocated_original(location) or location
    if "/AppTranslocation/" not in str(bundle) and _can_write(bundle.parent):
        return bundle, ""
    for folder in folders or APPLICATION_FOLDERS:
        if _can_write(folder):
            return Path(folder) / bundle.name, ""
    return None, (f"This account can't write to {bundle.parent} or to the Applications folder. "
                  "Download the update from GitHub and install it manually.")


def install_support(location=None, system=None):
    """``(True, "")`` when this copy can be updated, else ``(False, reason)``."""
    target, reason = install_target(location, system)
    return target is not None, reason


def install_command(package, location, pid, system=None, directory=None, target=None):
    """``(program, arguments)`` that finishes the update after process *pid* exits.

    On macOS the update goes to *target* (default: ``install_target(location)``).
    """
    key = platform_key(system)
    if key == "windows":
        installer = str(package).replace("'", "''")
        script = (f"Wait-Process -Id {int(pid)} -ErrorAction SilentlyContinue; "
                  f"Start-Process -FilePath '{installer}' -ArgumentList '{WINDOWS_INSTALLER_ARGS}'")
        return "powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-WindowStyle", "Hidden", "-Command", script]
    if key == "macos":
        if target is None:
            target, reason = install_target(location, system)
            if target is None:
                raise UpdateError(reason)
        script = write_macos_installer(package, target, pid, directory, current=location)
        return "/bin/sh", [str(script)]
    raise UpdateError("Automatic installation isn't supported on this platform.")


_MACOS_SCRIPT = """#!/bin/sh
# Finish an SV Face ID update: wait for the running copy to quit, copy the new
# app bundle out of the downloaded disk image to TARGET, then open it.  On any
# failure the previous bundle is kept (or restored), the copy that was running
# (CURRENT) is reopened, and RESULT says what went wrong so the app can tell.
PID={pid}
DMG={dmg}
TARGET={target}
CURRENT={current}
RESULT={result}
exec >>{log} 2>&1
echo "$(date) installing $DMG into $TARGET"
fail() {{
  echo "$1"
  printf '%s\\n' "$1" > "$RESULT"
  open "$CURRENT"
  exit 1
}}
while kill -0 "$PID" 2>/dev/null; do sleep 0.5; done
MOUNT=$(mktemp -d /tmp/sv-face-id-update.XXXXXX) || fail "Couldn't create a temporary folder."
if ! hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MOUNT" "$DMG"; then
  rmdir "$MOUNT" 2>/dev/null
  fail "Couldn't open the downloaded disk image."
fi
SOURCE=$(find "$MOUNT" -maxdepth 1 -name "*.app" | head -n 1)
mkdir -p "$(dirname "$TARGET")"
rm -rf "$TARGET.new"
if [ -z "$SOURCE" ] || ! ditto "$SOURCE" "$TARGET.new"; then
  rm -rf "$TARGET.new"
  hdiutil detach "$MOUNT" -quiet; rmdir "$MOUNT" 2>/dev/null
  fail "Couldn't copy the new version into $(dirname "$TARGET")."
fi
hdiutil detach "$MOUNT" -quiet; rmdir "$MOUNT" 2>/dev/null
if [ -e "$TARGET" ]; then
  rm -rf "$TARGET.old"
  if ! mv "$TARGET" "$TARGET.old"; then
    rm -rf "$TARGET.new"
    fail "Couldn't replace $TARGET."
  fi
  if ! mv "$TARGET.new" "$TARGET"; then
    mv "$TARGET.old" "$TARGET"
    fail "Couldn't replace $TARGET, so the previous version was kept."
  fi
  rm -rf "$TARGET.old"
elif ! mv "$TARGET.new" "$TARGET"; then
  rm -rf "$TARGET.new"
  fail "Couldn't move the new version into $(dirname "$TARGET")."
fi
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null
printf 'ok\\n' > "$RESULT"
open "$TARGET"
"""


def write_macos_installer(package, target, pid, directory=None, current=None):
    """Write the install script; *current* is the copy to reopen if installing fails."""
    directory = Path(directory or DOWNLOAD_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "install-update.sh"
    quote = lambda value: shlex.quote(str(value))
    script.write_text(_MACOS_SCRIPT.format(
        pid=int(pid), dmg=quote(package), target=quote(target), current=quote(current or target),
        result=quote(directory / RESULT_FILE), log=quote(directory / "install-update.log")),
        encoding="utf-8")
    script.chmod(0o755)
    return script


@dataclass(frozen=True)
class InstallOutcome:
    """How the last update attempt ended (see ``take_install_outcome``)."""
    version: str
    installed: bool
    reason: str = ""
    target: str = ""


def record_pending_install(version, target, directory=None):
    """Remember the update being installed, so the next start can report on it."""
    directory = Path(directory or DOWNLOAD_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / RESULT_FILE).unlink(missing_ok=True)
    (directory / PENDING_FILE).write_text(
        json.dumps({"version": str(version), "target": str(target or "")}), encoding="utf-8")


def note_install_failure(message, directory=None):
    """Record why the installer never ran; the script normally reports for itself."""
    directory = Path(directory or DOWNLOAD_DIR)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / RESULT_FILE).write_text(str(message) + "\n", encoding="utf-8")
    except OSError:
        logging.debug("could not record the install failure", exc_info=True)


def take_install_outcome(directory=None, current=__version__):
    """The outcome of the last update attempt, reported once; None when there was none.

    The running version is the proof: an update counts as installed only when
    this copy is at least the version that was being installed.
    """
    directory = Path(directory or DOWNLOAD_DIR)
    pending, result = directory / PENDING_FILE, directory / RESULT_FILE
    if not pending.exists():
        return None
    try:
        data = json.loads(pending.read_text(encoding="utf-8"))
        message = result.read_text(encoding="utf-8").strip() if result.exists() else ""
    except (OSError, ValueError):
        data, message = {}, ""
    for path in (pending, result):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logging.debug("could not clear %s", path, exc_info=True)
    data = data if isinstance(data, dict) else {}
    version, target = str(data.get("version") or ""), str(data.get("target") or "")
    if not version:
        return None
    if not is_newer(version, current):
        return InstallOutcome(version, True, target=target)
    if message == "ok":
        reason = (f"Version {version} was installed in {folder_label(Path(target).parent)}, "
                  "but an older copy was opened. Quit it and open SV Face ID from there.")
    else:
        reason = message or "The installer didn't finish."
    return InstallOutcome(version, False, reason, target)
