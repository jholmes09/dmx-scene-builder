#!/bin/bash
# Build "DMX Scene Builder.app" and "DMX Scene Builder.dmg" into ./dist (run from the repo root).
# Needs: python3 with pyinstaller installed (pip install pyinstaller).
# Signing: ad-hoc by default. Developer ID signing + notarization switch on when these are set:
#   SIGN_IDENTITY   e.g. "Developer ID Application: Name (TEAMID)"   (the workflow finds it in the keychain)
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD   -> also notarize and staple the dmg
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
NAME="DMX Scene Builder"
rm -rf build dist

ARCHS=$(lipo -archs "$($PY -c 'import sys;print(sys.executable)')" 2>/dev/null || true)
if [[ "$ARCHS" == *x86_64* && "$ARCHS" == *arm64* ]]; then export TARGET_ARCH=universal2; fi
echo "Python archs: $ARCHS  -> target: ${TARGET_ARCH:-native}"

"$PY" -m PyInstaller --noconfirm --distpath dist --workpath build packaging/DMXSceneBuilder.spec
APP="dist/$NAME.app"
rm -rf "dist/$NAME"   # the one-folder build PyInstaller leaves beside the .app

if [[ -n "${SIGN_IDENTITY:-}" ]]; then
  echo "Signing with Developer ID: $SIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp --entitlements packaging/entitlements.plist -s "$SIGN_IDENTITY" "$APP"
else
  echo "Ad-hoc signing (no Developer ID configured)"
  codesign --force --deep -s - "$APP"
fi
codesign --verify --deep --strict "$APP"

STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
DMG="dist/$NAME.dmg"
hdiutil create -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

if [[ -n "${SIGN_IDENTITY:-}" ]]; then
  codesign --force --timestamp -s "$SIGN_IDENTITY" "$DMG"
  if [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
    echo "Notarizing (this takes a few minutes)..."
    xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD" --wait
    xcrun stapler staple "$DMG"
  fi
fi
ls -la dist
