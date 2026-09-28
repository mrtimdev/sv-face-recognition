Here's how to build for each platform:

macOS

# One command:
./scripts/build.sh
This produces dist/SV Face ID.app — a double-clickable macOS app bundle.

To make a DMG installer:


hdiutil create -volname 'SV Face ID' -srcfolder 'dist/SV Face ID.app' \
  -ov -format UDZO 'dist/SV-Face-ID-1.0.3.dmg'
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


iscc scripts\installer.iss
Produces dist\SV-Face-ID-Setup-1.0.3.exe — a standard Windows installer with desktop shortcut.

Recognition and PAD ONNX assets plus licenses are bundled by `build.spec`.
Rebuild an existing app bundle to include the new backend. An older bundle
continues to use its old code until replaced. Preserve the user-data directory;
old dlib templates require photo migration or re-enrollment (see README).

## CI/CD (GitHub Actions)

Push a version tag to automatically build both platforms and create a GitHub Release:

    git tag v1.0.3
    git push origin v1.0.3

This triggers `.github/workflows/build-release.yml` which:
- Builds macOS `.app` and packages it as a `.dmg`
- Builds Windows `.exe` and creates an Inno Setup installer
- Creates a GitHub Release with both artifacts attached
