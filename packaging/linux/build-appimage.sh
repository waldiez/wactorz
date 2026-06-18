#!/usr/bin/env bash
# Build the self-contained Wactorz AppImage (bundles PySide6/QtWebEngine).
#
# Requires: pyinstaller. appimagetool is auto-downloaded to ~/.local/bin if it
# is not already on PATH (override with APPIMAGETOOL=/path/to/appimagetool).
# Build on the OLDEST glibc you support (e.g. an Ubuntu 22.04 container) so the
# AppImage runs on as many hosts as possible.
set -euo pipefail

export QT_API=PySide6

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# Read the version straight from the file — importing the wactorz package pulls
# in the whole backend stack (psutil, …), which the build host's system python
# need not have installed.
VERSION="$(sed -n 's/.*__version__ *= *"\([^"]*\)".*/\1/p' "$ROOT/wactorz/_version.py")"
DIST="$ROOT/dist"
APPDIR="$DIST/Wactorz.AppDir"
# AppImages are not cross-built (PyInstaller freezes for the host arch), so the
# arch is the host's unless explicitly overridden. Build the aarch64 AppImage on
# an aarch64 machine.
ARCH="${ARCH:-$(uname -m)}"
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"

# Ensure appimagetool is available; if not, fetch the official AppImage for this
# arch into ~/.local/bin (and add it to PATH for this build).
if ! command -v "$APPIMAGETOOL" >/dev/null 2>&1; then
    BIN_DIR="$HOME/.local/bin"
    TOOL="$BIN_DIR/appimagetool"
    if [ ! -x "$TOOL" ]; then
        echo "==> appimagetool not found — downloading for $ARCH"
        mkdir -p "$BIN_DIR"
        wget -O "$TOOL" \
            "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"
        chmod +x "$TOOL"
    fi
    APPIMAGETOOL="$TOOL"
    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *) export PATH="$BIN_DIR:$PATH" ;;
    esac
    # appimagetool is itself an AppImage and needs FUSE to run; build hosts
    # (old-glibc containers) often lack it, so self-extract instead.
    export APPIMAGE_EXTRACT_AND_RUN=1
fi

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
    # extra. desktop-qt adds PySide6 (the AppImage's Qt/QtWebEngine backend),
    # which [desktop] no longer pins. WACTORZ_FRONTEND_STALE keeps the build hook
    # from rebuilding the SPA.
    WACTORZ_FRONTEND_STALE=99999999 "$VENV/bin/pip" install -e "$ROOT[all,desktop-qt]"
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

# Size trims (no functionality loss):
#  - drop QtWebEngine's DevTools resources (we never open devtools);
#  - keep only English Qt translations.
# (No symbol stripping: stripping Qt/WebEngine plugins can break GL context
#  creation — not worth the few MB.)
find "$DIST/wactorz-desktop" -name 'qtwebengine_devtools_resources.pak' -delete 2>/dev/null || true
while IFS= read -r tdir; do
    find "$tdir" -maxdepth 1 -name '*.qm' ! -name 'en*' -delete 2>/dev/null || true
    echo "    pruned translations in $tdir"
done < <(find "$DIST/wactorz-desktop" -type d -name translations 2>/dev/null)

# Never ship the graphics-driver stack: the GPU driver must match the HOST, so a
# bundled Mesa/libGL shadows the host's and breaks hardware GL (blank window,
# "EGL not available" — fine under LIBGL_ALWAYS_SOFTWARE=1, broken otherwise).
# Delete any that PyInstaller bundled so the host's are always used.
find "$DIST/wactorz-desktop" -type f \( \
    -name 'libGL.so*'    -o -name 'libEGL.so*'   -o -name 'libGLX*.so*' -o \
    -name 'libGLdispatch.so*' -o -name 'libgbm.so*'  -o -name 'libdrm.so*' -o \
    -name 'libglapi.so*' -o -name 'libgallium*.so*' -o -name 'libEGL_*.so*' -o \
    -name 'libdrm_*.so*' \) -delete 2>/dev/null || true
echo "    excluded host graphics-stack libs"

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
