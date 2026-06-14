#!/usr/bin/env bash
# Build the self-contained Wactorz AppImage (bundles PySide6/QtWebEngine).
#
# Requires: pyinstaller, and appimagetool on PATH (or set APPIMAGETOOL).
# Build on the OLDEST glibc you support (e.g. an Ubuntu 22.04 container) so the
# AppImage runs on as many hosts as possible.
set -euo pipefail

export QT_API=PySide6

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
VERSION="$(cd "$ROOT" && python3 -c 'import wactorz._version as v; print(v.__version__)')"
DIST="$ROOT/dist"
APPDIR="$DIST/Wactorz.AppDir"
# AppImages are not cross-built (PyInstaller freezes for the host arch), so the
# arch is the host's unless explicitly overridden. Build the aarch64 AppImage on
# an aarch64 machine.
ARCH="${ARCH:-$(uname -m)}"
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"

# Freeze from a dedicated, isolated venv (in gitignored .local/) so the bundle
# is reproducible and never polluted by whatever's in the dev env. Delete
# .local/build-venv to refresh its deps.
VENV="$ROOT/.local/build-venv"
if [ ! -x "$VENV/bin/pyinstaller" ]; then
    echo "==> [0/3] Creating build venv: $VENV"
    mkdir -p "$ROOT/.local"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip pyinstaller >/dev/null
    # [all] = LLM providers + integrations + desktop, minus the heavy ml/torch
    # extra. WACTORZ_FRONTEND_STALE keeps the build hook from rebuilding the SPA.
    WACTORZ_FRONTEND_STALE=99999999 "$VENV/bin/pip" install -e "$ROOT[all]"
fi

echo "==> [1/3] Freezing with PyInstaller"
"$VENV/bin/pyinstaller" --noconfirm --clean \
    --distpath "$DIST" --workpath "$DIST/pyi-build" \
    "$ROOT/packaging/pyinstaller/wactorz-desktop.spec"

# Trim QtWebEngine's non-English locale .pak files (~tens of MB). The exact path
# varies by PySide6 layout, so glob for it and keep only en-US.
while IFS= read -r loc; do
    find "$loc" -name '*.pak' ! -name 'en-US.pak' -delete
    echo "    pruned locales in $loc"
done < <(find "$DIST/wactorz-desktop" -type d -name qtwebengine_locales 2>/dev/null)

echo "==> [2/3] Assembling AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/metainfo"
cp -r "$DIST/wactorz-desktop" "$APPDIR/usr/bin/wactorz-desktop"
install -m 755 "$HERE/AppRun"          "$APPDIR/AppRun"
install -m 644 "$HERE/io.waldiez.wactorz.desktop" "$APPDIR/io.waldiez.wactorz.desktop"
install -m 644 "$ROOT/wactorz/desktop/assets/icon.png" "$APPDIR/wactorz.png"
# metainfo — version-stamped from the shared template
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$(date +%F)/" \
    "$HERE/io.waldiez.wactorz.metainfo.template.xml" \
    > "$APPDIR/usr/share/metainfo/io.waldiez.wactorz.metainfo.xml"

echo "==> [3/3] Building AppImage"
ARCH="$ARCH" "$APPIMAGETOOL" "$APPDIR" "$DIST/Wactorz-$ARCH.AppImage"
echo "==> Done: $DIST/Wactorz-$ARCH.AppImage"
