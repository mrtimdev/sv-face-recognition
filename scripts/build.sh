#!/usr/bin/env bash
# Build SV Face ID for macOS.
#
# Usage:
#   chmod +x scripts/build.sh
#   ./scripts/build.sh
#
# Output: dist/SV Face ID.app  (macOS app bundle)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Activating venv..."
source venv/bin/activate

echo "==> Installing PyInstaller..."
pip install pyinstaller --quiet

echo "==> Cleaning previous build..."
rm -rf build/ dist/

echo "==> Building..."
pyinstaller build.spec --noconfirm

echo ""
echo "==> Done!"
if [ -d "dist/SV Face ID.app" ]; then
    echo "    macOS app:  dist/SV Face ID.app"
    echo ""
    echo "    To create a DMG installer:"
    echo "      hdiutil create -volname 'SV Face ID' -srcfolder 'dist/SV Face ID.app' \\"
    echo "        -ov -format UDZO 'dist/SV-Face-ID-2.0.0.dmg'"
    echo ""
    echo "    To sign for distribution:"
    echo "      codesign --deep --force --sign 'Developer ID Application: YOUR NAME' 'dist/SV Face ID.app'"
elif [ -d "dist/SV Face ID" ]; then
    echo "    App folder: dist/SV Face ID/"
    echo "    Run with:   './dist/SV Face ID/SV Face ID'"
fi
