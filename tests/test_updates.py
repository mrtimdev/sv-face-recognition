"""Application updates: versioning, GitHub release checks, verified downloads,
installers, the Settings > Updates page and the startup prompt.

Nothing here touches the network: GitHub is replaced by fake ``urlopen``
callables, and the page's workers call patched ``updates`` functions.
"""
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.test_dashboard import _app, temp_settings
from tests.test_dashboard_access import wait_until

ROOT = Path(__file__).resolve().parent.parent


class _Response(io.BytesIO):
    def __init__(self, payload, length=None):
        super().__init__(payload)
        self.headers = {"Content-Length": str(len(payload) if length is None else length)}


def _opener(payload, length=None):
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        return _Response(payload, length)
    opener.calls = calls
    return opener


def _release_json(tag="v1.2.0", assets=None):
    return json.dumps({
        "tag_name": tag, "name": tag, "body": "## Changes\n* Faster startup",
        "published_at": "2026-09-30T04:07:20Z",
        "html_url": f"https://github.com/mrtimdev/sv-face-recognition/releases/tag/{tag}",
        "assets": assets if assets is not None else [
            {"name": "SV-Face-ID-v1.2.0-mac.dmg", "size": 10, "digest": "sha256:ab",
             "browser_download_url": "https://example.test/mac.dmg"},
            {"name": "helper.exe", "size": 5, "browser_download_url": "https://example.test/helper.exe"},
            {"name": "SV-Face-ID-Setup-1.2.0.exe", "size": 20, "digest": "sha256:cd",
             "browser_download_url": "https://example.test/setup.exe"},
        ]}).encode()


class VersionTests(unittest.TestCase):
    def test_versions_compare_numerically(self):
        from face_attendance.updates import is_newer, parse_version
        self.assertEqual(parse_version("v1.0.10"), (1, 0, 10))
        self.assertTrue(is_newer("v1.0.10", "1.0.9"))
        self.assertFalse(is_newer("1.0.5", "1.0.5"))
        self.assertFalse(is_newer("nightly", "1.0.5"))

    def test_the_app_reports_the_single_version(self):
        from face_attendance.dashboard.app import APP_VERSION
        from face_attendance.version import __version__
        self.assertEqual(APP_VERSION, f"v{__version__}")

    def test_set_version_stamps_from_a_tag(self):
        spec = importlib.util.spec_from_file_location("set_version", ROOT / "scripts" / "set_version.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version.py"
            target.write_text('"""doc"""\n__version__ = "1.0.0"\n', encoding="utf-8")
            with patch.object(module, "VERSION_FILE", target):
                self.assertEqual(module.stamp("v2.3.4"), "2.3.4")
                self.assertEqual(module.current(), "2.3.4")
                self.assertIn('"""doc"""', target.read_text(encoding="utf-8"))
                with self.assertRaises(SystemExit):
                    module.stamp("main")

    def test_release_files_take_the_version_from_the_tag(self):
        installer = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("OutputBaseFilename=SV-Face-ID-Setup-{#MyAppVersion}", installer)
        self.assertIn("AppVersion={#MyAppVersion}", installer)
        self.assertIn("AppId=SV Face ID", installer)
        self.assertIn("{param:relaunch|0}", installer)
        self.assertNotRegex(installer, r"(?m)^(AppVersion|OutputBaseFilename)=.*\d+\.\d+\.\d+")
        workflow = (ROOT / ".github" / "workflows" / "build-release.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count("scripts/set_version.py \""), 2)
        self.assertIn('iscc "/DMyAppVersion=$version" scripts\\installer.iss', workflow)
        spec = (ROOT / "build.spec").read_text(encoding="utf-8")
        self.assertIn('"version.py"', spec)
        self.assertNotRegex(spec, r'(?m)^VERSION = "\d')


class ReleaseCheckTests(unittest.TestCase):
    def test_latest_release_and_platform_assets(self):
        from face_attendance.updates import check_latest
        release = check_latest(opener=_opener(_release_json()), system="win32")
        self.assertEqual((release.version, release.tag), ("1.2.0", "v1.2.0"))
        self.assertEqual(release.asset.name, "SV-Face-ID-Setup-1.2.0.exe")
        self.assertEqual(release.asset.digest, "sha256:cd")
        self.assertIn("Faster startup", release.notes)
        mac = check_latest(opener=_opener(_release_json()), system="darwin")
        self.assertEqual(mac.asset.name, "SV-Face-ID-v1.2.0-mac.dmg")
        self.assertIsNone(check_latest(opener=_opener(_release_json()), system="linux").asset)

    def test_the_installer_named_after_the_release_wins(self):
        from face_attendance.updates import pick_asset
        assets = [{"name": "SV-Face-ID-Setup-1.0.3.exe", "size": 1, "browser_download_url": "old"},
                  {"name": "SV-Face-ID-Setup-1.0.6.exe", "size": 1, "browser_download_url": "new"}]
        self.assertEqual(pick_asset(assets, "win32", "1.0.6").url, "new")
        self.assertEqual(pick_asset(assets[:1], "win32", "1.0.6").url, "old")   # only one: still offered

    def test_failures_become_friendly_messages(self):
        from face_attendance.updates import UpdateError, check_latest

        def failing(error):
            def opener(request, timeout=None):
                raise error
            return opener

        cases = (
            (urllib.error.HTTPError("u", 404, "nf", {}, None), "No published release"),
            (urllib.error.HTTPError("u", 403, "rl", {}, None), "limiting"),
            (urllib.error.URLError("offline"), "Couldn't reach GitHub"),
        )
        for error, message in cases:
            with self.assertRaises(UpdateError) as caught:
                check_latest(opener=failing(error))
            self.assertIn(message, str(caught.exception))
        with self.assertRaises(UpdateError):
            check_latest(opener=_opener(_release_json(tag="latest-build")))


class DownloadTests(unittest.TestCase):
    PAYLOAD = b"installer-bytes" * 5000

    def _asset(self, name="SV-Face-ID-Setup-1.2.0.exe", digest=None, size=None):
        from face_attendance.updates import Asset
        digest = digest if digest is not None else "sha256:" + hashlib.sha256(self.PAYLOAD).hexdigest()
        return Asset(name=name, size=len(self.PAYLOAD) if size is None else size,
                     url="https://example.test/setup.exe", digest=digest)

    def test_verified_download_is_saved_and_reused(self):
        from face_attendance.updates import download
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            opener = _opener(self.PAYLOAD)
            path = download(self._asset(), directory, progress=lambda got, total: seen.append(got),
                            opener=opener)
            self.assertEqual(path.read_bytes(), self.PAYLOAD)
            self.assertEqual(seen[-1], len(self.PAYLOAD))
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), [path.name])
            # A verified copy on disk is reused without downloading again.
            again = download(self._asset(), directory, opener=opener)
            self.assertEqual((again, len(opener.calls)), (path, 1))

    def test_tampered_or_short_downloads_are_discarded(self):
        from face_attendance.updates import UpdateError, download
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UpdateError) as caught:
                download(self._asset(digest="sha256:" + "0" * 64), directory, opener=_opener(self.PAYLOAD))
            self.assertIn("checksum", str(caught.exception))
            with self.assertRaises(UpdateError):
                download(self._asset(size=len(self.PAYLOAD) + 1), directory, opener=_opener(self.PAYLOAD))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancelling_leaves_nothing_behind(self):
        from face_attendance.updates import UpdateCancelled, download
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(UpdateCancelled):
                download(self._asset(), directory, cancelled=lambda: True, opener=_opener(self.PAYLOAD))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_asset_names_cannot_escape_the_download_folder(self):
        from face_attendance.updates import download
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / "updates"
            path = download(self._asset(name="../../evil.exe"), folder, opener=_opener(self.PAYLOAD))
            self.assertEqual(path.parent, folder)
            self.assertEqual(path.name, "evil.exe")

    def test_stale_downloads_are_removed(self):
        from face_attendance.updates import remove_stale_downloads
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("SV-Face-ID-Setup-1.0.4.exe", "SV-Face-ID-Setup-9.9.9.exe", "x.exe.part"):
                (folder / name).write_bytes(b"x")
            self.assertEqual(remove_stale_downloads(folder, current="1.0.5"), 2)
            self.assertEqual([p.name for p in folder.iterdir()], ["SV-Face-ID-Setup-9.9.9.exe"])


class InstallTests(unittest.TestCase):
    def test_installed_location_per_platform(self):
        from face_attendance.updates import installed_location
        app = installed_location("/Applications/SV Face ID.app/Contents/MacOS/SV Face ID",
                                 system="darwin", frozen=True)
        self.assertEqual(app, Path("/Applications/SV Face ID.app"))
        folder = installed_location("/opt/SV Face ID/SV Face ID.exe", system="win32", frozen=True)
        self.assertEqual(folder, Path("/opt/SV Face ID"))
        self.assertIsNone(installed_location(system="darwin", frozen=False))

    def test_install_support_explains_why_not(self):
        from face_attendance.updates import install_support
        self.assertFalse(install_support(None, system="darwin")[0])
        self.assertIn("source", install_support(None, system="win32")[1])
        self.assertFalse(install_support(Path("/tmp/x"), system="linux")[0])
        self.assertTrue(install_support(Path("C:/Apps/SV Face ID"), system="win32")[0])

    def test_macos_updates_in_place_when_the_folder_is_writable(self):
        from face_attendance.updates import install_target
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "SV Face ID.app"
            self.assertEqual(install_target(app, system="darwin"), (app, ""))

    def test_a_read_only_copy_is_updated_into_applications(self):
        # Opened from the disk image: the volume (and macOS's translocated copy) is read-only.
        from face_attendance.updates import install_target
        with tempfile.TemporaryDirectory() as directory:
            volume, applications = Path(directory) / "Volume", Path(directory) / "Applications"
            volume.mkdir()
            volume.chmod(0o555)
            try:
                self.assertEqual(install_target(volume / "SV Face ID.app", system="darwin",
                                                folders=[applications]),
                                 (applications / "SV Face ID.app", ""))
                # Nowhere writable at all: explain instead of pretending.
                target, reason = install_target(volume / "SV Face ID.app", system="darwin",
                                                folders=[volume / "Applications"])
            finally:
                volume.chmod(0o755)
            self.assertIsNone(target)
            self.assertIn("can't write", reason)

    def test_a_translocated_app_resolves_to_its_original(self):
        from face_attendance import updates
        translocated = Path("/private/var/folders/x/T/AppTranslocation/ABC/d/SV Face ID.app")
        with tempfile.TemporaryDirectory() as directory:
            downloads, applications = Path(directory) / "Downloads", Path(directory) / "Applications"
            downloads.mkdir()
            # The original is in a writable folder: update it there.
            with patch.object(updates, "translocated_original", return_value=downloads / "SV Face ID.app"):
                self.assertEqual(updates.install_target(translocated, system="darwin",
                                                        folders=[applications])[0],
                                 downloads / "SV Face ID.app")
            # The original is on a disk image, or unknown: install into Applications.
            with patch.object(updates, "translocated_original",
                              return_value=Path("/Volumes/SV Face ID/SV Face ID.app")):
                self.assertEqual(updates.install_target(translocated, system="darwin",
                                                        folders=[applications])[0],
                                 applications / "SV Face ID.app")
                self.assertTrue(updates.opened_from_disk_image(translocated))
            with patch.object(updates, "translocated_original", return_value=None):
                self.assertEqual(updates.install_target(translocated, system="darwin",
                                                        folders=[applications])[0],
                                 applications / "SV Face ID.app")
        self.assertIsNone(updates.translocated_original("/Applications/SV Face ID.app"))
        self.assertFalse(updates.opened_from_disk_image(Path("/Applications/SV Face ID.app")))
        self.assertFalse(updates.opened_from_disk_image(None))

    def test_windows_waits_for_the_app_then_runs_the_installer_silently(self):
        from face_attendance.updates import install_command
        program, arguments = install_command(r"C:\Users\O'Neil\updates\SV-Face-ID-Setup-1.2.0.exe",
                                             None, 4321, system="win32")
        script = arguments[-1]
        self.assertEqual(program, "powershell.exe")
        self.assertIn("Wait-Process -Id 4321", script)
        self.assertIn("O''Neil", script)            # apostrophes escaped for PowerShell
        self.assertIn("/RELAUNCH=1", script)
        self.assertIn("/SILENT", script)

    def test_macos_script_swaps_the_bundle_after_exit(self):
        from face_attendance.updates import install_command
        with tempfile.TemporaryDirectory() as directory:
            program, arguments = install_command(
                Path(directory) / "SV Face ID v1.2.0.dmg",
                Path("/Volumes/SV Face ID/SV Face ID.app"), 4321, system="darwin",
                directory=directory, target=Path("/Applications/SV Face ID.app"))
            script = Path(arguments[0])
            text = script.read_text(encoding="utf-8")
            self.assertEqual(program, "/bin/sh")
            self.assertTrue(os.access(script, os.X_OK))
            self.assertIn("PID=4321", text)
            self.assertIn("TARGET='/Applications/SV Face ID.app'", text)
            self.assertIn("CURRENT='/Volumes/SV Face ID/SV Face ID.app'", text)
            self.assertIn('mv "$TARGET.old" "$TARGET"', text)   # restores on a failed swap
            self.assertEqual(subprocess.run(["/bin/sh", "-n", str(script)]).returncode, 0)

    @unittest.skipUnless(sys.platform == "darwin", "the install script uses macOS tools")
    def test_macos_script_installs_reports_and_recovers(self):
        """Runs the real script; only ``hdiutil`` and ``open`` are stand-ins."""
        from face_attendance.updates import RESULT_FILE, install_command
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stubs, opened = root / "stubs", root / "opened.log"
            stubs.mkdir()
            (stubs / "hdiutil").write_text(
                '#!/bin/sh\n'
                'if [ "$1" = attach ]; then\n'
                '  while [ "$1" != -mountpoint ]; do shift; done\n'
                '  mkdir -p "$2/SV Face ID.app/Contents" && echo 9.9.9 > "$2/SV Face ID.app/Contents/version"\n'
                'else rm -rf "$2/SV Face ID.app"; fi\n', encoding="utf-8")
            (stubs / "open").write_text(f'#!/bin/sh\necho "$1" >> "{opened}"\n', encoding="utf-8")
            for stub in stubs.iterdir():
                stub.chmod(0o755)
            finished = subprocess.Popen(["true"])
            finished.wait()
            current = root / "Volume" / "SV Face ID.app"
            env = dict(os.environ, PATH=f"{stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}")

            def install(target):
                opened.unlink(missing_ok=True)
                _, arguments = install_command(root / "update.dmg", current, finished.pid,
                                               system="darwin", directory=root, target=target)
                subprocess.run(["/bin/sh", *arguments], env=env, timeout=30, check=False)
                return ((root / RESULT_FILE).read_text(encoding="utf-8").strip(),
                        opened.read_text(encoding="utf-8").strip())

            target = root / "Applications" / "SV Face ID.app"
            self.assertEqual(install(target), ("ok", str(target)))          # fresh install
            self.assertEqual((target / "Contents" / "version").read_text().strip(), "9.9.9")
            (target / "old-file").write_text("x")
            self.assertEqual(install(target), ("ok", str(target)))          # replaces a copy
            self.assertFalse((target / "old-file").exists())
            self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["SV Face ID.app"])
            # A folder that can't be written: report why and reopen the running copy.
            target.parent.chmod(0o555)
            try:
                result, reopened = install(target)
            finally:
                target.parent.chmod(0o755)
            self.assertIn("Couldn't copy the new version", result)
            self.assertEqual(reopened, str(current))
            self.assertTrue((target / "Contents" / "version").exists(), "the installed copy is kept")

    def test_the_next_start_reports_whether_the_update_installed(self):
        from face_attendance.updates import (RESULT_FILE, note_install_failure,
                                             record_pending_install, take_install_outcome)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.assertIsNone(take_install_outcome(folder))
            record_pending_install("1.0.8", "/Applications/SV Face ID.app", folder)
            outcome = take_install_outcome(folder, current="1.0.8")
            self.assertEqual((outcome.version, outcome.installed), ("1.0.8", True))
            self.assertIsNone(take_install_outcome(folder, current="1.0.8"), "reported once")

            record_pending_install("1.0.8", "/Applications/SV Face ID.app", folder)
            (folder / RESULT_FILE).write_text("Couldn't copy the new version into /Applications.\n")
            outcome = take_install_outcome(folder, current="1.0.7")
            self.assertFalse(outcome.installed)
            self.assertEqual(outcome.reason, "Couldn't copy the new version into /Applications.")

            record_pending_install("1.0.8", "/Applications/SV Face ID.app", folder)
            (folder / RESULT_FILE).write_text("ok\n")       # installed, but an old copy was opened
            self.assertIn("older copy", take_install_outcome(folder, current="1.0.7").reason)

            record_pending_install("1.0.8", "", folder)
            note_install_failure("The installer couldn't be started.", folder)
            self.assertEqual(take_install_outcome(folder, current="1.0.7").reason,
                             "The installer couldn't be started.")

            record_pending_install("1.0.8", "", folder)
            self.assertEqual(take_install_outcome(folder, current="1.0.7").reason,
                             "The installer didn't finish.")
            (folder / "pending-install.json").write_text("{not json")
            self.assertIsNone(take_install_outcome(folder))
            self.assertEqual(list(folder.iterdir()), [])

    def test_unsupported_platforms_refuse(self):
        from face_attendance.updates import UpdateError, install_command
        with self.assertRaises(UpdateError):
            install_command("/tmp/x", None, 1, system="linux")


class UpdatesPageTests(unittest.TestCase):
    def setUp(self):
        _app()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = temp_settings(self._dir.name)
        self.settings._path = Path(self._dir.name) / "settings.json"

    def _release(self, version="9.9.9"):
        from face_attendance.updates import Asset, Release
        return Release(version=version, tag=f"v{version}", title=f"v{version}",
                       notes="* Better things", published_at="2026-09-30T04:07:20Z",
                       page_url="https://example.test",
                       asset=Asset("SV-Face-ID-Setup-9.9.9.exe", 104857600, "https://example.test/x"))

    def _page(self):
        from face_attendance.dashboard.screens.updates import UpdatesPage
        page = UpdatesPage(self.settings)
        page.show()
        self.addCleanup(page.close)
        return page

    def _check(self, page, release):
        with patch("face_attendance.updates.check_latest", return_value=release):
            page.check_now()
            self.assertTrue(wait_until(lambda: not page.is_busy()))
        _app().processEvents()

    def test_up_to_date(self):
        from face_attendance.settings import Settings
        from face_attendance.version import __version__
        page = self._page()
        self._check(page, self._release(__version__))
        self.assertEqual(page.status_title.text(), "You're up to date")
        self.assertTrue(Settings.load(self.settings._path).update_last_checked)

    def test_new_version_can_be_skipped_and_unskipped(self):
        from face_attendance.settings import Settings
        found = []
        page = self._page()
        page.updateFound.connect(found.append)
        with patch("face_attendance.updates.installed_location", return_value=Path(self._dir.name)), \
                patch("face_attendance.updates.install_support", return_value=(True, "")):
            self._check(page, self._release())
            self.assertEqual(page.status_title.text(), "Version 9.9.9 is available")
            self.assertIn("100.0 MB", page.status_body.text())
            self.assertFalse(page.install_button.isHidden())
            self.assertFalse(page.skip_button.isHidden())
            self.assertEqual([release.version for release in found], ["9.9.9"])
            page.skip_button.click()
            self.assertEqual(Settings.load(self.settings._path).update_skipped_version, "9.9.9")
            self.assertIn("(skipped)", page.status_title.text())
            self.assertEqual(page.skipped_label.text(), "Skipping version 9.9.9")
            self._check(page, self._release())
            self.assertEqual(len(found), 1, "a skipped version is not offered again")
            page.unskip_button.click()
            self.assertEqual(Settings.load(self.settings._path).update_skipped_version, "")

    def test_auto_check_preference_is_saved(self):
        from face_attendance.settings import Settings
        page = self._page()
        page.auto_toggle.click()
        self.assertFalse(Settings.load(self.settings._path).update_auto_check)

    def test_download_then_install_hands_over_the_verified_package(self):
        requested = []
        package = Path(self._dir.name) / "SV-Face-ID-Setup-9.9.9.exe"
        package.write_bytes(b"x")

        def fake_download(asset, progress=None, cancelled=None, **_):
            progress(50, 100)
            progress(100, 100)
            return package

        page = self._page()
        page.installRequested.connect(lambda release, path: requested.append((release.version, path)))
        with patch("face_attendance.updates.installed_location", return_value=Path(self._dir.name)), \
                patch("face_attendance.updates.install_support", return_value=(True, "")):
            self._check(page, self._release())
            with patch("face_attendance.updates.download", side_effect=fake_download):
                page.install_button.click()
                self.assertTrue(wait_until(lambda: requested))
        self.assertEqual(requested, [("9.9.9", str(package))])
        self.assertEqual(page.status_title.text(), "Ready to install version 9.9.9")

    def test_failed_download_offers_a_retry(self):
        from face_attendance.updates import UpdateError
        page = self._page()
        with patch("face_attendance.updates.installed_location", return_value=Path(self._dir.name)), \
                patch("face_attendance.updates.install_support", return_value=(True, "")):
            self._check(page, self._release())
            with patch("face_attendance.updates.download", side_effect=UpdateError("The download failed its checksum.")):
                page.install_button.click()
                self.assertTrue(wait_until(lambda: page.status_title.text() == "Download failed"))
        self.assertIn("checksum", page.status_body.text())
        self.assertEqual(page.install_button.text().strip(), "Try again")

    def test_source_runs_explain_instead_of_installing(self):
        page = self._page()
        with patch("face_attendance.updates.installed_location", return_value=None):
            self._check(page, self._release())
        self.assertTrue(page.install_button.isHidden())
        self.assertIn("running from source", page.status_body.text())

    def test_a_copy_opened_from_the_disk_image_installs_into_applications(self):
        dmg_copy = Path("/Volumes/SV Face ID/SV Face ID.app")
        with patch("face_attendance.updates.installed_location", return_value=dmg_copy), \
                patch("face_attendance.updates.install_target",
                      return_value=(Path("/Applications/SV Face ID.app"), "")):
            page = self._page()
            self._check(page, self._release())
        self.assertIn("opened from the disk image", page.platform_label.text())
        self.assertIn("running from its disk image", page.status_body.text())
        self.assertIn("installed in Applications", page.status_body.text())
        self.assertFalse(page.install_button.isHidden())

    def test_the_startup_prompt_never_installs_what_cant_be_installed(self):
        page = self._page()
        with patch("face_attendance.updates.installed_location", return_value=Path("/x/SV Face ID.app")), \
                patch("face_attendance.updates.install_target",
                      return_value=(None, "This account can't write to /x or to the Applications folder.")), \
                patch("face_attendance.updates.download", side_effect=AssertionError("downloaded")):
            page.install_update(self._release())
            self.assertFalse(page.is_busy())
        self.assertIn("can't write", page.status_body.text())
        self.assertTrue(page.install_button.isHidden())

    def test_a_failed_update_is_explained_until_it_is_resolved(self):
        from face_attendance.updates import InstallOutcome
        from face_attendance.version import __version__
        page = self._page()
        page.set_install_problem(InstallOutcome("9.9.9", False, "Couldn't copy the new version."))
        with patch("face_attendance.updates.installed_location", return_value=Path(self._dir.name)), \
                patch("face_attendance.updates.install_target", return_value=(Path(self._dir.name), "")):
            self._check(page, self._release())
        self.assertFalse(page.problem_banner.isHidden())
        self.assertEqual(page.problem_title.text(), "Version 9.9.9 wasn't installed")
        self.assertEqual(page.problem_body.text(), "Couldn't copy the new version.")
        self.assertEqual(page.install_button.text().strip(), "Try again")
        self._check(page, self._release(__version__))
        self.assertTrue(page.problem_banner.isHidden())

    def test_release_note_links_keep_the_whole_url(self):
        from dataclasses import replace
        from face_attendance.dashboard.screens.updates import linkify
        url = "https://github.com/mrtimdev/sv-face-recognition/compare/v1.0.5...v1.0.7"
        self.assertEqual(linkify(f"**Full Changelog**: {url}."), f"**Full Changelog**: <{url}>.")
        self.assertEqual(linkify(f"[compare]({url})"), f"[compare]({url})")      # already a link
        page = self._page()
        with patch("face_attendance.updates.installed_location", return_value=None):
            self._check(page, replace(self._release(), notes=f"**Full Changelog**: {url}"))
        block, anchors = page.notes.document().begin(), set()
        while block.isValid():
            fragments = block.begin()
            while not fragments.atEnd():
                href = fragments.fragment().charFormat().anchorHref()
                if href:
                    anchors.add(href)
                fragments += 1
            block = block.next()
        self.assertEqual(anchors, {url})


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        _app()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.settings = temp_settings(self._dir.name)
        self.settings._path = Path(self._dir.name) / "settings.json"

    def _window(self, current_user=None):
        from face_attendance.dashboard.app import MainWindow
        window = MainWindow(self.settings, use_lock=False, current_user=current_user)
        window.show()

        def cleanup():
            if window.isVisible():
                window._exit_done = True
                window.close()
            _app().processEvents()
        self.addCleanup(cleanup)
        return window

    def _release(self):
        from face_attendance.updates import Asset, Release
        return Release("9.9.9", "v9.9.9", "v9.9.9", "", "", "",
                       Asset("SV-Face-ID-Setup-9.9.9.exe", 1, "https://example.test/x"))

    def test_installing_shuts_down_then_starts_the_installer(self):
        from PyQt6.QtCore import QProcess
        window = self._window()
        with patch("face_attendance.updates.install_command", return_value=("installer", ["/SILENT"])), \
                patch.object(QProcess, "startDetached", return_value=(True, 99)) as start:
            window._install_update(self._release(), "/tmp/SV-Face-ID-Setup-9.9.9.exe")
            overlay = window._exit.overlay
            self.assertEqual(overlay.title_label.text(), "Installing update")
            self.assertIn("Version 9.9.9 will be installed", overlay.subtitle_label.text())
            self.assertEqual(overlay.steps[-1].label.text(), "Starting the installer")
            self.assertTrue(wait_until(lambda: not window.isVisible(), timeout=10))
        start.assert_called_once_with("installer", ["/SILENT"])
        self.assertFalse(window.engine.running)

    def test_startup_prompt_can_skip_the_version(self):
        from face_attendance.settings import Settings
        window = self._window()
        page = window.screens[5].updates_page
        page._release = self._release()
        window._prompt_for_update = True
        with patch("face_attendance.updates.install_support", return_value=(True, "")):
            window._on_update_found(self._release())
        prompt = window._update_prompt
        self.assertIsNotNone(prompt)
        self.assertIn("9.9.9", prompt.message.text())
        self.assertEqual([item.kind for item in window.notifications.items()], ["update"])
        prompt.skip_button.click()
        _app().processEvents()
        self.assertEqual(Settings.load(self.settings._path).update_skipped_version, "9.9.9")
        # Found again later (e.g. a manual check): no second prompt or notification.
        window._on_update_found(self._release())
        self.assertIsNone(window._update_prompt)
        self.assertEqual(len(window.notifications), 1)

    def test_no_prompt_when_installing_cannot_work_here(self):
        window = self._window()
        window._prompt_for_update = True
        with patch("face_attendance.updates.install_support", return_value=(False, "read-only")):
            window._on_update_found(self._release())
        self.assertIsNone(window._update_prompt)
        self.assertEqual([item.kind for item in window.notifications.items()], ["update"])

    def test_a_failed_update_is_reported_at_the_next_start(self):
        from face_attendance.updates import InstallOutcome
        window = self._window()
        window._report_install_outcome(InstallOutcome(
            "9.9.9", False, "Couldn't copy the new version into /Applications."))
        [notice] = window.notifications.items()
        self.assertEqual((notice.kind, notice.title), ("update_failed", "Version 9.9.9 wasn't installed"))
        self.assertIn("Couldn't copy", notice.body)
        self.assertFalse(window.screens[5].updates_page.problem_banner.isHidden())
        # The automatic check finds the same version: the prompt says what went wrong.
        window._prompt_for_update = True
        with patch("face_attendance.updates.install_support", return_value=(True, "")):
            window._on_update_found(self._release())
        prompt = window._update_prompt
        self.assertIsNotNone(prompt.problem)
        self.assertEqual(prompt.install_button.text(), "Try again")
        self.assertEqual(len(window.notifications), 1, "no second 'is available' notice")
        prompt.later_button.click()
        _app().processEvents()

    def test_a_successful_update_is_confirmed(self):
        from face_attendance.updates import InstallOutcome
        window = self._window()
        window._report_install_outcome(InstallOutcome("9.9.9", True))
        window._report_install_outcome(None)
        [notice] = window.notifications.items()
        self.assertEqual((notice.kind, notice.title), ("update", "Updated to version 9.9.9"))
        self.assertTrue(window.screens[5].updates_page.problem_banner.isHidden())

    def test_only_accounts_that_manage_settings_are_asked(self):
        from face_attendance.users import User
        user = User(uuid="u", username="dara", full_name="Dara", phone="", email="",
                    password_hash="x", role="user", permissions=["view_dashboard"])
        window = self._window(current_user=user)
        with patch.object(window.screens[5].updates_page, "check_now") as check:
            window._auto_check_updates()
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
