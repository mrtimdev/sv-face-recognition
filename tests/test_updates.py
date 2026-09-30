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
        with tempfile.TemporaryDirectory() as directory:
            locked = Path(directory) / "Applications"
            locked.mkdir()
            locked.chmod(0o555)
            try:
                supported, reason = install_support(locked / "SV Face ID.app", system="darwin")
            finally:
                locked.chmod(0o755)
            self.assertFalse(supported)
            self.assertIn("can't write", reason)

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
                Path(directory) / "SV Face ID v1.2.0.dmg", Path("/Applications/SV Face ID.app"),
                4321, system="darwin", directory=directory)
            script = Path(arguments[0])
            text = script.read_text(encoding="utf-8")
            self.assertEqual(program, "/bin/sh")
            self.assertTrue(os.access(script, os.X_OK))
            self.assertIn("PID=4321", text)
            self.assertIn("TARGET='/Applications/SV Face ID.app'", text)
            self.assertIn('mv "$TARGET.old" "$TARGET"', text)   # restores on a failed swap
            self.assertEqual(subprocess.run(["/bin/sh", "-n", str(script)]).returncode, 0)

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
