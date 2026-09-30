Here's how to build for each platform:

macOS

# One command:
./scripts/build.sh
This produces dist/SV Face ID.app — a double-clickable macOS app bundle.

To make a DMG installer:


hdiutil create -volname 'SV Face ID' -srcfolder 'dist/SV Face ID.app' \
  -ov -format UDZO "dist/SV-Face-ID-v$(python scripts/set_version.py)-mac.dmg"
To sign for distribution (requires Apple Developer ID):


codesign --deep --force --sign 'Developer ID Application: YOUR NAME' 'dist/SV Face ID.app'
Windows
Prerequisites (one-time setup on Windows machine):

Python 3.9+
OpenCV, NumPy and PyQt6 wheels (no dlib/CMake compilation needed)
pip install -r requirements.txt in a venv
Build:


.\scripts\build.ps1
This produces dist\SV Face ID\SV Face ID.exe.

To make an installer — install Inno Setup, then:


iscc /DMyAppVersion=1.0.6 scripts\installer.iss
Produces dist\SV-Face-ID-Setup-1.0.6.exe — a standard Windows installer with desktop shortcut.
(Use the version from `python scripts/set_version.py`; CI passes it automatically.)

Recognition and PAD ONNX assets plus licenses are bundled by `build.spec`.
Rebuild an existing app bundle to include the new backend. An older bundle
continues to use its old code until replaced. Preserve the user-data directory;
old dlib templates require photo migration or re-enrollment (see README).

## CI/CD (GitHub Actions)

### Build a Windows installer without publishing a release

Once the updated workflow is on the repository's default branch, open
**Actions → Build & Release → Run workflow**. Select the branch containing
the installer fix (for example, `uniface-version`) and click **Run workflow**.
This manual run builds Windows only. When it succeeds, download the
`windows-installer` artifact from the run summary and extract the setup `.exe`.

### Recovering the failed v1.0.3 build

The original `v1.0.3` tag points to a commit without `SourceDir=..` in
`scripts/installer.iss`. Inno Setup therefore searches for `icon.ico` and
`dist` inside `scripts`, causing the installer step to fail. The branch fix
resolves these paths from the repository root, including the output path used
by the artifact upload step.

Re-running that old tagged workflow still uses the old commit. Use the manual
branch build above, or create a new release tag that includes the fix.

### One version, taken from the tag

`face_attendance/version.py` is the only place the version lives. On a `v*`
tag the workflow runs `python scripts/set_version.py <tag>` before building, so
the app (sidebar, splash, Settings › Updates), the macOS `Info.plist`, the DMG
and the Windows installer (`SV-Face-ID-Setup-<version>.exe`, and the version
Windows shows under Installed apps) all match the tag. Nothing needs editing by
hand. (Releases v1.0.4 and v1.0.5 shipped an installer still named 1.0.3
because the version used to be typed into `installer.iss`.)

### Publish a release

Push a version tag to automatically build both platforms and create a GitHub Release:

    python scripts/set_version.py v1.0.6    # optional: keeps source runs in step
    git commit -am "Release v1.0.6"
    git tag v1.0.6
    git push origin HEAD v1.0.6

This triggers `.github/workflows/build-release.yml` which:
- Builds macOS `.app` and packages it as a `.dmg`
- Builds Windows `.exe` and creates an Inno Setup installer
- Creates a GitHub Release with both artifacts attached

## In-app updates

The installed app checks `https://api.github.com/repos/mrtimdev/sv-face-recognition/releases/latest`
(no token needed while the repository is public):

- **Settings › Updates** shows the running version, *Check for updates*,
  *Download & install*, *Skip this version* / *Stop skipping*, the release notes,
  and *Check for updates automatically* (on by default).
- With auto-check on, the installed app checks a few seconds after start and asks
  accounts that can manage settings: *Install update*, *Skip this version* or
  *Later*. A skipped version is not offered again; newer ones still are.
- Downloads go to `<user data>/updates/` and must match the SHA-256 digest GitHub
  publishes for the asset; otherwise nothing is installed.
- Installing shuts the dashboard down (camera, recognition, queued attendance)
  behind the progress card, then:
  - **Windows**: runs `SV-Face-ID-Setup-<version>.exe /SILENT /RELAUNCH=1`
    after the app has exited; the installer reopens the app.
  - **macOS**: copies `SV Face ID.app` out of the DMG over the running copy
    (keeping the old one if the swap fails) and opens it. A copy opened straight
    from its disk image can't be replaced — the volume is read-only, and macOS
    runs such apps from a read-only "translocated" copy — so the update is
    installed in `/Applications` (or `~/Applications`) and opened from there.
- The next start reports what happened: *Updated to version X*, or *Version X
  wasn't installed* with the reason (also shown in Settings › Updates, and the
  next prompt offers *Try again*). The macOS script logs to
  `<user data>/updates/install-update.log`.
- Running from source, the check works but nothing is installed.

The first release that contains the updater must be installed by hand once;
earlier versions (up to v1.0.5) cannot update themselves.

