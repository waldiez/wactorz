#!/usr/bin/env bash
# Build, sign and notarize the macOS app (.app + .dmg). Apple Silicon (arm64).
#
# Prereqs (on macOS):
#   - Xcode command line tools (codesign, hdiutil, xcrun notarytool/stapler)
#   - A "Developer ID Application" certificate in the login keychain
#   - A notarytool keychain profile, created once:
#       xcrun notarytool store-credentials wactorz-notary \
#         --apple-id <apple-id> --team-id <TEAMID> --password <app-specific-pw>
#
# Env:
#   MACOS_SIGN_IDENTITY   e.g. "Developer ID Application: Your Name (TEAMID)"
#   MACOS_NOTARY_PROFILE  keychain profile name (default: wactorz-notary)
#
# Packages whatever is in static/app; run 'make build-frontend' first if the SPA
# changed. The .dmg is arm64-only (built on Apple Silicon; no cross-build).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DIST="$ROOT/dist"
VERSION="$(cd "$ROOT" && python3 -c 'import wactorz._version as v; print(v.__version__)')"
APP="$DIST/Wactorz.app"
DMG="$DIST/Wactorz-$VERSION-arm64.dmg"
IDENTITY="${MACOS_SIGN_IDENTITY:?set MACOS_SIGN_IDENTITY to your 'Developer ID Application: …' identity}"
NOTARY_PROFILE="${MACOS_NOTARY_PROFILE:-wactorz-notary}"

# Freeze from a dedicated, isolated venv (in gitignored .local/) for a clean,
# reproducible bundle. Delete .local/build-venv to refresh its deps.
VENV="$ROOT/.local/build-venv"
if [ ! -x "$VENV/bin/pyinstaller" ]; then
    echo "==> [0/5] Creating build venv: $VENV"
    mkdir -p "$ROOT/.local"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip pyinstaller >/dev/null
    WACTORZ_FRONTEND_STALE=99999999 "$VENV/bin/pip" install -e "$ROOT[all]"
fi

echo "==> [1/5] Freezing .app with PyInstaller"
"$VENV/bin/pyinstaller" --noconfirm --clean \
    --distpath "$DIST" --workpath "$DIST/pyi-build" \
    "$ROOT/packaging/pyinstaller/wactorz-desktop.spec"

echo "==> [2/5] Code-signing (hardened runtime)"
# --deep is convenient for PyInstaller's many nested dylibs; if notarization
# flags unsigned nested code, sign inner binaries individually instead.
codesign --force --deep --options runtime --timestamp \
    --entitlements "$HERE/entitlements.plist" \
    --sign "$IDENTITY" "$APP"
codesign --verify --strict --verbose=2 "$APP"

echo "==> [3/5] Building .dmg"
rm -f "$DMG"
hdiutil create -volname "Wactorz" -srcfolder "$APP" -ov -format UDZO "$DMG"
codesign --force --timestamp --sign "$IDENTITY" "$DMG"

echo "==> [4/5] Notarizing (can take a few minutes)"
xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait

echo "==> [5/5] Stapling"
xcrun stapler staple "$DMG"
xcrun stapler staple "$APP" || true

echo "==> Done: $DMG"
