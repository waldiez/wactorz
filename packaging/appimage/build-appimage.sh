#!/usr/bin/env bash
# Build the self-contained Wactorz AppImage (bundles PySide6/QtWebEngine).
#
# Requires: pyinstaller, and appimagetool on PATH (or set APPIMAGETOOL).
# Build on the OLDEST glibc you support (e.g. an Ubuntu 22.04 container) so the
# AppImage runs on as many hosts as possible.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DIST="$ROOT/dist"
APPDIR="$DIST/Wactorz.AppDir"
ARCH="${ARCH:-x86_64}"
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"

echo "==> [1/3] Freezing with PyInstaller"
pyinstaller --noconfirm --clean \
    --distpath "$DIST" --workpath "$DIST/pyi-build" \
    "$ROOT/packaging/pyinstaller/wactorz-desktop.spec"

echo "==> [2/3] Assembling AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
cp -r "$DIST/wactorz-desktop" "$APPDIR/usr/bin/wactorz-desktop"
install -m 755 "$HERE/AppRun"          "$APPDIR/AppRun"
install -m 644 "$HERE/wactorz.desktop" "$APPDIR/wactorz.desktop"
install -m 644 "$ROOT/wactorz/desktop/assets/icon.png" "$APPDIR/wactorz.png"

echo "==> [3/3] Building AppImage"
ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$DIST/Wactorz-$ARCH.AppImage"
echo "==> Done: $DIST/Wactorz-$ARCH.AppImage"
