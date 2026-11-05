"""Settings > Updates: the running version, update checks, download, install and skip.

Network work runs on worker threads.  A verified download is handed to the
main window (``installRequested``), which shuts the dashboard down behind the
progress card and starts the platform installer.
"""
import platform
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
                             QTextBrowser, QVBoxLayout, QWidget)

from ... import updates
from ...version import __version__
from ..icons import apply_button_icon
from ..theme import palette
from ..threads import settle
from ..widgets import Card, IconTile, ToggleSwitch
from ..widgets.notifications import relative_time
from ..widgets.overlay import WindowOverlay

# A bare URL in release notes, e.g. GitHub's ".../compare/v1.0.5...v1.0.7".
_BARE_URL = re.compile(r"(?<![<(\[])\bhttps?://[^\s<>()\[\]]+")


def platform_label():
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        return "macOS • " + ("Apple Silicon" if machine in ("arm64", "aarch64") else "Intel")
    if system == "Windows":
        return "Windows • " + ("64-bit" if machine.endswith("64") else machine or "32-bit")
    return system or "Unknown system"


def _local_time(iso_text):
    try:
        moment = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _megabytes(size):
    return f"{size / 1_048_576:.1f} MB"


def linkify(notes):
    """Wrap bare URLs in ``<...>`` so Markdown links them whole.

    Qt's Markdown ends a bare link at "...", so GitHub's changelog link opened
    ``compare/v1.0.5`` instead of ``compare/v1.0.5...v1.0.7``.
    """
    def wrap(match):
        url = match.group(0)
        trimmed = url.rstrip(".,;:!?")
        return f"<{trimmed}>{url[len(trimmed):]}"
    return _BARE_URL.sub(wrap, notes)


def placement_note(location, target):
    """How the update gets installed when it can't replace the running copy, else ""."""
    if location is None or target is None or Path(target) == Path(location):
        return ""
    where = updates.folder_label(Path(target).parent)
    if updates.opened_from_disk_image(location):
        return (f"SV Face ID is running from its disk image, so the update will be installed "
                f"in {where} and opened from there.")
    return (f"This copy can't be replaced where it is, so the update will be installed "
            f"in {where} and opened from there.")


class _CheckWorker(QThread):
    done = pyqtSignal(object, str)          # Release (or None), error text

    def run(self):
        try:
            release = updates.check_latest()
        except updates.UpdateError as exc:
            self.done.emit(None, str(exc))
            return
        except Exception as exc:            # never let a surprise kill the thread silently
            self.done.emit(None, f"Unexpected error: {exc}")
            return
        self.done.emit(release, "")


class _DownloadWorker(QThread):
    progress = pyqtSignal(object, object)   # received, total bytes (may exceed 32 bits)
    done = pyqtSignal(str, str)             # verified path, error text ("cancelled" when cancelled)

    def __init__(self, asset):
        super().__init__()
        self._asset = asset
        self._cancel = threading.Event()
        self._last = 0.0

    def cancel(self):
        self._cancel.set()

    def _report(self, received, total):
        now = time.monotonic()
        if now - self._last >= 0.05 or received >= total:
            self._last = now
            self.progress.emit(received, total)

    def run(self):
        try:
            path = updates.download(self._asset, progress=self._report, cancelled=self._cancel.is_set)
        except updates.UpdateCancelled:
            self.done.emit("", "cancelled")
            return
        except updates.UpdateError as exc:
            self.done.emit("", str(exc))
            return
        except Exception as exc:
            self.done.emit("", f"Unexpected error: {exc}")
            return
        self.done.emit(str(path), "")


class UpdatesPage(QWidget):
    """One page of the Settings screen."""

    installRequested = pyqtSignal(object, str)   # Release, verified installer path
    updateFound = pyqtSignal(object)             # a newer release that is not skipped

    def __init__(self, settings, theme="light", parent=None):
        super().__init__(parent)
        self.settings = settings
        self._theme = theme
        self._release = None
        self._problem = None             # InstallOutcome of a failed update, shown until resolved
        self._package = ""
        self._install_after_download = False
        self._check_worker = None
        self._download_worker = None
        self._download_started = 0.0
        self._build()
        self._show_idle()

    # --- construction ------------------------------------------------------
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(14)
        scroll.setWidget(holder)
        outer.addWidget(scroll)

        self.about_card = Card("About this app", "", icon="face-id", theme=self._theme)
        about = QHBoxLayout()
        about.setSpacing(16)
        self.app_tile = IconTile("face-id", tone="blue", size=56, theme=self._theme)
        about.addWidget(self.app_tile, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(3)
        name = QLabel("SV Face ID")
        name.setObjectName("dialogTitle")
        self.version_label = QLabel(f"Version {__version__}")
        self.version_label.setObjectName("cellTitle")
        self.platform_label = QLabel("")
        self.platform_label.setObjectName("fieldHelp")
        self.checked_label = QLabel("")
        self.checked_label.setObjectName("fieldHelp")
        for widget in (name, self.version_label, self.platform_label, self.checked_label):
            text.addWidget(widget)
        about.addLayout(text, 1)
        self.about_card.add_layout(about)
        column.addWidget(self.about_card)

        self.update_card = Card("Software update", "Releases are published on GitHub.",
                                icon="download", theme=self._theme)
        status = QHBoxLayout()
        status.setSpacing(14)
        self.status_tile = IconTile("refresh", tone="blue", size=44, theme=self._theme)
        status.addWidget(self.status_tile, 0, Qt.AlignmentFlag.AlignTop)
        status_text = QVBoxLayout()
        status_text.setSpacing(3)
        self.status_title = QLabel("")
        self.status_title.setObjectName("formSectionTitle")
        self.status_body = QLabel("")
        self.status_body.setObjectName("formSectionHint")
        self.status_body.setWordWrap(True)
        status_text.addWidget(self.status_title)
        status_text.addWidget(self.status_body)
        status.addLayout(status_text, 1)
        self.update_card.add_layout(status)

        self.problem_banner = QFrame()
        self.problem_banner.setObjectName("banner")
        self.problem_banner.setProperty("tone", "warn")
        problem = QVBoxLayout(self.problem_banner)
        problem.setContentsMargins(12, 8, 12, 8)
        problem.setSpacing(2)
        self.problem_title = QLabel("")
        self.problem_title.setObjectName("bannerTitle")
        self.problem_body = QLabel("")
        self.problem_body.setObjectName("bannerBody")
        self.problem_body.setWordWrap(True)
        problem.addWidget(self.problem_title)
        problem.addWidget(self.problem_body)
        self.problem_banner.hide()
        self.update_card.add(self.problem_banner)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.hide()
        self.update_card.add(self.progress)

        self.notes = QTextBrowser()
        self.notes.setObjectName("releaseNotes")
        self.notes.setOpenExternalLinks(True)
        self.notes.setMaximumHeight(170)
        self.notes.hide()
        self.update_card.add(self.notes)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.check_button = self._button("  Check for updates", "controlButton", self.check_now)
        self.install_button = self._button("  Download && install", "primary", self._on_install_clicked)
        self.skip_button = self._button("Skip this version", "ghostButton", self._skip_current)
        self.unskip_button = self._button("Stop skipping", "ghostButton", self.clear_skip)
        self.cancel_button = self._button("Cancel download", "controlButton", self._cancel_download)
        self.folder_button = self._button("Show in folder", "ghostButton", self._show_in_folder)
        self.page_button = self._button("View on GitHub  ", "linkButton", self._open_release_page)
        self.page_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._buttons = {"check": self.check_button, "install": self.install_button,
                         "skip": self.skip_button, "unskip": self.unskip_button,
                         "cancel": self.cancel_button, "folder": self.folder_button,
                         "page": self.page_button}
        for key in ("install", "check", "cancel", "skip", "unskip", "folder"):
            buttons.addWidget(self._buttons[key])
        buttons.addStretch(1)
        buttons.addWidget(self.page_button)
        self.update_card.add_layout(buttons)
        column.addWidget(self.update_card)

        self.prefs_card = Card("Preferences", "", icon="sliders", theme=self._theme)
        auto = QHBoxLayout()
        auto_text = QVBoxLayout()
        auto_text.setSpacing(2)
        auto_title = QLabel("Check for updates automatically")
        auto_title.setObjectName("cellTitle")
        auto_hint = QLabel("When the installed app starts. You'll be asked before anything is installed.")
        auto_hint.setObjectName("fieldHelp")
        auto_hint.setWordWrap(True)
        auto_text.addWidget(auto_title)
        auto_text.addWidget(auto_hint)
        auto.addLayout(auto_text, 1)
        self.auto_toggle = ToggleSwitch(bool(self.settings.update_auto_check), theme=self._theme)
        self.auto_toggle.toggled.connect(self._set_auto_check)
        auto.addWidget(self.auto_toggle, 0, Qt.AlignmentFlag.AlignVCenter)
        self.prefs_card.add_layout(auto)
        skipped = QHBoxLayout()
        self.skipped_label = QLabel("")
        self.skipped_label.setObjectName("fieldHelp")
        skipped.addWidget(self.skipped_label, 1)
        self.skipped_clear = self._button("Stop skipping", "linkButton", self.clear_skip)
        skipped.addWidget(self.skipped_clear)
        self.prefs_card.add_layout(skipped)
        column.addWidget(self.prefs_card)
        column.addStretch(1)
        self._cards = (self.about_card, self.update_card, self.prefs_card)
        self._apply_icons()
        self._sync_preferences()

    def _button(self, text, object_name, slot):
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    # --- public api ----------------------------------------------------------
    def set_settings(self, settings):
        self.settings = settings
        self._sync_preferences()

    def is_busy(self):
        return any(worker is not None and worker.isRunning()
                   for worker in (self._check_worker, self._download_worker))

    def check_now(self):
        if self.is_busy():
            return
        self._install_after_download = False
        self._set_status("blue", "refresh", "Checking for updates…",
                         "Contacting GitHub for the latest release.")
        self._show_buttons()
        self.progress.setRange(0, 0)
        self.progress.show()
        self.notes.hide()
        worker = _CheckWorker()
        worker.done.connect(self._on_checked)
        self._check_worker = worker
        worker.start()

    def install_update(self, release=None):
        """Download (if needed) and then install; used by the startup prompt."""
        if release is not None:
            self._release = release
        if self._release is None or not self._can_install():
            return
        self._install_after_download = True
        self._start_download()

    def set_install_problem(self, outcome):
        """Show why the last update didn't install (``InstallOutcome``); None clears it."""
        self._problem = outcome
        if outcome is None:
            self.problem_banner.hide()
            return
        self.problem_title.setText(f"Version {outcome.version} wasn't installed")
        self.problem_body.setText(outcome.reason)
        self.problem_banner.show()

    def skip_version(self, release):
        self._release = release
        self._save(update_skipped_version=release.version)
        self._render_release()

    def clear_skip(self):
        self._save(update_skipped_version="")
        if self._release is not None and self._release.is_newer():
            self._render_release()

    def current_release(self):
        return self._release

    # --- checking ------------------------------------------------------------
    def _on_checked(self, release, error):
        self.progress.hide()
        if error:
            self._set_status("red", "alert", "Couldn't check for updates", error)
            self._show_buttons("check")
            self.check_button.setText("  Try again")
            return
        self.check_button.setText("  Check for updates")
        self._release = release
        self._save(update_last_checked=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if not release.is_newer():
            self.set_install_problem(None)
            self._set_status("green", "check-circle", "You're up to date",
                             f"Version {__version__} is the newest release.")
            self._show_buttons("check", "page")
            return
        self._render_release()
        if release.version != self.settings.update_skipped_version:
            self.updateFound.emit(release)

    def _render_release(self):
        release = self._release
        published = _local_time(release.published_at)
        facts = [f"You have {__version__}."]
        if published is not None:
            facts.append(f"Released {published.strftime('%d %b %Y')}")
        if release.asset is not None and release.asset.size:
            facts.append(_megabytes(release.asset.size))
        body = " • ".join(facts)
        location = updates.installed_location()
        target, reason = updates.install_target(location)
        note = placement_note(location, target)
        skipped = release.version == self.settings.update_skipped_version
        if self._problem is not None and self._problem.version != release.version:
            self.set_install_problem(None)      # about an older release; no longer relevant
        if release.asset is None:
            self._set_status("orange", "alert", f"Version {release.version} is available",
                             body + "\nThis release has no installer for this computer.")
            self._show_buttons("check", "page")
        elif target is None:
            self._set_status("orange", "download", f"Version {release.version} is available",
                             f"{body}\n{reason}")
            self._show_buttons("check", "page")
        elif skipped:
            self._set_status("orange", "download", f"Version {release.version} is available (skipped)",
                             body + "\nYou chose to skip this version; it won't be offered at startup."
                             + (f"\n{note}" if note else ""))
            self.install_button.setText("  Install anyway")
            self._show_buttons("install", "unskip", "page")
        else:
            self._set_status("blue", "download", f"Version {release.version} is available",
                             body + (f"\n{note}" if note else ""))
            self.install_button.setText("  Try again" if self._problem is not None
                                        else "  Download && install")
            self._show_buttons("install", "skip", "page")
        notes = release.notes or "No release notes were published for this version."
        self.notes.setMarkdown(linkify(notes))
        self.notes.show()

    def _can_install(self):
        """False when this copy can't be updated; the page then says why."""
        if updates.install_target(updates.installed_location())[0] is not None:
            return True
        if self._release is not None:
            self._render_release()
        return False

    # --- downloading and installing --------------------------------------------
    def _on_install_clicked(self):
        if not self._can_install():
            return
        if self._package and self._release is not None:
            self.installRequested.emit(self._release, self._package)
            return
        self._install_after_download = True
        self._start_download()

    def _start_download(self):
        release = self._release
        if release is None or release.asset is None or self.is_busy():
            return
        self._set_status("blue", "download", f"Downloading version {release.version}…",
                         f"0 of {_megabytes(release.asset.size)}")
        self._show_buttons("cancel")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.show()
        self._download_started = time.monotonic()
        worker = _DownloadWorker(release.asset)
        worker.progress.connect(self._on_progress)
        worker.done.connect(self._on_downloaded)
        self._download_worker = worker
        worker.start()

    def _on_progress(self, received, total):
        total = total or (self._release.asset.size if self._release and self._release.asset else 0)
        if total:
            self.progress.setValue(int(1000 * min(1.0, received / total)))
        elapsed = max(0.001, time.monotonic() - self._download_started)
        speed = received / elapsed
        self.status_body.setText(f"{_megabytes(received)} of {_megabytes(total)} • "
                                 f"{speed / 1_048_576:.1f} MB/s")

    def _cancel_download(self):
        if self._download_worker is not None:
            self._download_worker.cancel()

    def _on_downloaded(self, path, error):
        self.progress.hide()
        if error == "cancelled":
            self._install_after_download = False
            self._render_release()
            return
        if error:
            self._install_after_download = False
            self._set_status("red", "alert", "Download failed", error)
            self.install_button.setText("  Try again")
            self._show_buttons("install", "skip", "page")
            return
        self._package = path
        location = updates.installed_location()
        target = updates.install_target(location)[0]
        if placement_note(location, target):
            closing = (f"SV Face ID will close, install the update in "
                       f"{updates.folder_label(Path(target).parent)} and open it from there.")
        else:
            closing = "SV Face ID will close, install the update and reopen on its own."
        self._set_status("green", "check-circle", f"Ready to install version {self._release.version}",
                         f"Downloaded and verified. {closing}")
        self.install_button.setText("  Install and restart")
        self._show_buttons("install", "folder", "page")
        if self._install_after_download:
            self._install_after_download = False
            self.installRequested.emit(self._release, self._package)

    def _skip_current(self):
        if self._release is not None:
            self.skip_version(self._release)

    def _show_in_folder(self):
        if self._package:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(updates.DOWNLOAD_DIR)))

    def _open_release_page(self):
        release = self._release
        url = release.page_url if release and release.page_url else \
            f"https://github.com/{updates.REPOSITORY}/releases"
        QDesktopServices.openUrl(QUrl(url))

    # --- state helpers ------------------------------------------------------------
    def _show_idle(self):
        self._set_status("blue", "refresh", "Check for updates",
                         f"Version {__version__} is installed.")
        self._show_buttons("check", "page")

    def _set_status(self, tone, glyph, title, body):
        self.status_tile.set_icon(glyph)
        self.status_tile.set_tone(tone, self._theme)
        self.status_title.setText(title)
        self.status_body.setText(body)

    def _show_buttons(self, *visible):
        for key, button in self._buttons.items():
            button.setVisible(key in visible)

    def _set_auto_check(self, enabled):
        self._save(update_auto_check=bool(enabled))

    def _save(self, **values):
        for key, value in values.items():
            setattr(self.settings, key, value)
        try:
            self.settings.save()
        except OSError:
            pass    # the choice still applies for this session
        self._sync_preferences()

    def _sync_preferences(self):
        location = updates.installed_location()
        if location is None:
            where = "running from source"
        elif updates.opened_from_disk_image(location):
            where = "opened from the disk image"
        else:
            where = "installed app"
        self.platform_label.setText(f"{platform_label()} • {where}")
        checked = _local_time(self.settings.update_last_checked)
        self.checked_label.setText(f"Last checked: {relative_time(checked)}"
                                   if checked else "Not checked yet")
        self.auto_toggle.blockSignals(True)
        self.auto_toggle.setChecked(bool(self.settings.update_auto_check))
        self.auto_toggle.blockSignals(False)
        self.auto_toggle._progress = 1.0 if self.settings.update_auto_check else 0.0
        self.auto_toggle.update()
        skipped = self.settings.update_skipped_version
        self.skipped_label.setText(f"Skipping version {skipped}" if skipped
                                   else "No versions are skipped.")
        self.skipped_clear.setVisible(bool(skipped))

    def set_theme(self, theme):
        self._theme = theme
        for card in self._cards:
            card.set_theme(theme)
        self.app_tile.set_tone("blue", theme)
        self.auto_toggle.set_theme(theme)
        self._apply_icons()

    def _apply_icons(self):
        c = palette(self._theme)
        apply_button_icon(self.check_button, "refresh", c["text_secondary"], 14)
        apply_button_icon(self.install_button, "download", "#FFFFFF", 15)
        apply_button_icon(self.page_button, "arrow-right", c["primary"], 14)

    def closeEvent(self, event):
        if self._download_worker is not None:
            self._download_worker.cancel()
        settle(self._check_worker)
        settle(self._download_worker)
        super().closeEvent(event)


class UpdatePromptDialog(WindowOverlay):
    """Startup notice for a new release: install now, skip this version, or later.

    *problem* is why the previous attempt at this version didn't install.
    """

    INSTALL, SKIP, LATER = "install", "skip", "later"

    def __init__(self, release, theme="light", parent=None, problem=""):
        super().__init__(theme, parent, dismissible=True, title="Update available")
        self.choice = self.LATER
        column = self.column
        column.addWidget(IconTile("download", tone="blue", size=60, theme=theme), 0,
                         Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(16)
        title = QLabel("Update available")
        title.setObjectName("dialogTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        column.addSpacing(6)
        self.message = QLabel(f"SV Face ID {release.version} is ready to install. "
                              f"You have {__version__}.")
        self.message.setObjectName("dialogSubtitle")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        column.addWidget(self.message)
        column.addSpacing(8)
        size = f" • {_megabytes(release.asset.size)}" if release.asset and release.asset.size else ""
        location = updates.installed_location()
        target = updates.install_target(location)[0]
        if placement_note(location, target):
            action = (f"The app closes, installs the update in "
                      f"{updates.folder_label(Path(target).parent)} and opens it from there")
        else:
            action = "The app closes, installs the update and reopens by itself"
        self.hint = QLabel(action + size + ".")
        self.hint.setObjectName("fieldHelp")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setWordWrap(True)
        column.addWidget(self.hint)
        self.problem = None
        if problem:
            column.addSpacing(12)
            self.problem = QFrame()
            self.problem.setObjectName("banner")
            self.problem.setProperty("tone", "warn")
            box = QVBoxLayout(self.problem)
            box.setContentsMargins(12, 8, 12, 8)
            box.setSpacing(2)
            heading = QLabel("The last attempt didn't install")
            heading.setObjectName("bannerTitle")
            detail = QLabel(problem)
            detail.setObjectName("bannerBody")
            detail.setWordWrap(True)
            box.addWidget(heading)
            box.addWidget(detail)
            column.addWidget(self.problem)
        column.addSpacing(20)
        self.install_button = QPushButton("Try again" if problem else "Install update")
        self.install_button.setObjectName("primary")
        self.install_button.setMinimumHeight(42)
        self.install_button.setDefault(True)
        self.install_button.clicked.connect(self._install)
        column.addWidget(self.install_button)
        column.addSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.skip_button = QPushButton("Skip this version")
        self.skip_button.setObjectName("ghostButton")
        self.skip_button.clicked.connect(self._skip)
        self.later_button = QPushButton("Later")
        self.later_button.setObjectName("ghostButton")
        self.later_button.clicked.connect(self.reject)
        for button in (self.install_button, self.skip_button, self.later_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.skip_button, 1)
        row.addWidget(self.later_button, 1)
        column.addLayout(row)

    def _install(self):
        self.choice = self.INSTALL
        self.accept()

    def _skip(self):
        self.choice = self.SKIP
        self.accept()
