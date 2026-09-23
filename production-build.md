Here's how to build for each platform:

macOS

# One command:
./scripts/build.sh
This produces dist/SV Face ID.app — a double-clickable macOS app bundle.

To make a DMG installer:


hdiutil create -volname 'SV Face ID' -srcfolder 'dist/SV Face ID.app' \
  -ov -format UDZO 'dist/SV-Face-ID-2.0.0.dmg'
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
Produces dist\SV-Face-ID-Setup-1.0.0.exe — a standard Windows installer with desktop shortcut.

Recognition and PAD ONNX assets plus licenses are bundled by `build.spec`.
Rebuild an existing app bundle to include the new backend. An older bundle
continues to use its old code until replaced. Preserve the user-data directory;
old dlib templates require photo migration or re-enrollment (see README).
