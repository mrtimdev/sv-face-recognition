"""Application updates from GitHub Releases: check, download, verify, install.

``check_latest()`` reads the newest published release, ``download()`` streams
the asset for this platform and verifies it against the SHA-256 digest GitHub
publishes (nothing unverified is ever installed), and ``install_command()``
returns the process that finishes the update once this app has exited:

* Windows - the Inno Setup installer, run silently with ``/RELAUNCH=1`` so it
  reopens the app (started only after this process has quit);
* macOS - a small shell script that swaps the ``.app`` bundle from the
  downloaded disk image, then reopens it.

Running from source there is nothing to install over; the check still works.
"""
import hashlib
import json
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


def install_support(location=None, system=None):
    """``(True, "")`` when this copy can update itself, else ``(False, reason)``."""
    key = platform_key(system)
    if key is None:
        return False, "Automatic updates are available on Windows and macOS."
    if location is None:
        return False, ("You're running from source. Updates install automatically in the "
                       "packaged app; here, pull the latest code instead.")
    if key == "macos" and not os.access(Path(location).parent, os.W_OK):
        return False, (f"This account can't write to {Path(location).parent}. "
                       "Move the app to a folder you can write to, or install the update manually.")
    return True, ""


def install_command(package, location, pid, system=None, directory=None):
    """``(program, arguments)`` that finishes the update after process *pid* exits."""
    key = platform_key(system)
    if key == "windows":
        installer = str(package).replace("'", "''")
        script = (f"Wait-Process -Id {int(pid)} -ErrorAction SilentlyContinue; "
                  f"Start-Process -FilePath '{installer}' -ArgumentList '{WINDOWS_INSTALLER_ARGS}'")
        return "powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass",
                                  "-WindowStyle", "Hidden", "-Command", script]
    if key == "macos":
        return "/bin/sh", [str(write_macos_installer(package, location, pid, directory))]
    raise UpdateError("Automatic installation isn't supported on this platform.")


_MACOS_SCRIPT = """#!/bin/sh
# Finish an SV Face ID update: wait for the running copy to quit, swap in the
# new app bundle from the downloaded disk image, then reopen it.  The previous
# bundle is restored if anything goes wrong.
PID={pid}
DMG={dmg}
TARGET={target}
exec >>{log} 2>&1
echo "$(date) installing $DMG over $TARGET"
while kill -0 "$PID" 2>/dev/null; do sleep 0.5; done
MOUNT=$(mktemp -d /tmp/sv-face-id-update.XXXXXX)
if ! hdiutil attach -nobrowse -readonly -noautoopen -mountpoint "$MOUNT" "$DMG"; then
  echo "could not mount the disk image"; open "$TARGET"; exit 1
fi
SOURCE=$(find "$MOUNT" -maxdepth 1 -name "*.app" | head -n 1)
rm -rf "$TARGET.new"
if [ -z "$SOURCE" ] || ! ditto "$SOURCE" "$TARGET.new"; then
  echo "could not copy the new app"; rm -rf "$TARGET.new"
  hdiutil detach "$MOUNT" -quiet; open "$TARGET"; exit 1
fi
hdiutil detach "$MOUNT" -quiet || true
rm -rf "$TARGET.old"
if mv "$TARGET" "$TARGET.old"; then
  if mv "$TARGET.new" "$TARGET"; then
    rm -rf "$TARGET.old"
  else
    echo "swap failed, restoring the previous version"; mv "$TARGET.old" "$TARGET"
  fi
fi
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
open "$TARGET"
"""


def write_macos_installer(package, location, pid, directory=None):
    directory = Path(directory or DOWNLOAD_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "install-update.sh"
    script.write_text(_MACOS_SCRIPT.format(
        pid=int(pid), dmg=shlex.quote(str(package)), target=shlex.quote(str(location)),
        log=shlex.quote(str(directory / "install-update.log"))), encoding="utf-8")
    script.chmod(0o755)
    return script
