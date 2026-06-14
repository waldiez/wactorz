#!/usr/bin/env bash
# Build native .deb and .rpm packages of the Wactorz desktop app.
#
# Unlike the AppImage, these are NOT frozen: they ship a relocatable venv in
# /opt/wactorz (without PySide6) and declare the host's WebKit2GTK + pygobject
# as dependencies, which pywebview then auto-selects (gtk backend).
#
# Requires: python3 (>=3.10), fpm. Build .deb on Debian/Ubuntu and .rpm on
# Fedora so the runtime deps resolve on the target distro.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
DIST="$ROOT/dist"
STAGE="$DIST/stage"
PREFIX="/opt/wactorz"
VENV="$STAGE$PREFIX/venv"
VERSION="$(cd "$ROOT" && python3 -c 'import wactorz._version as v; print(v.__version__)')"

echo "==> [1/4] Staging venv (no PySide6 — uses system WebKit2GTK)"
rm -rf "$STAGE"
mkdir -p "$STAGE$PREFIX" "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/512x512/apps"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel >/dev/null
# Core app + GTK-friendly desktop deps. Deliberately omit pywebview's [pyside6]
# extra; the host provides WebKit2GTK via the declared package dependencies.
"$VENV/bin/pip" install pywebview pystray pillow plyer "$ROOT"

# Relocate venv from the build path ($STAGE$PREFIX) to the install path ($PREFIX):
# rewrite the absolute shebangs/paths baked into bin/ console scripts.
grep -rIl "$STAGE$PREFIX" "$VENV/bin" 2>/dev/null \
    | xargs -r sed -i "s|$STAGE$PREFIX|$PREFIX|g"

echo "==> [2/4] Launcher + desktop entry + icon"
cat > "$STAGE/usr/bin/wactorz-desktop" <<EOF
#!/bin/sh
exec $PREFIX/venv/bin/wactorz-desktop "\$@"
EOF
chmod 755 "$STAGE/usr/bin/wactorz-desktop"
install -m 644 "$HERE/wactorz.desktop" "$STAGE/usr/share/applications/wactorz.desktop"
install -m 644 "$ROOT/wactorz/desktop/assets/icon.png" \
    "$STAGE/usr/share/icons/hicolor/512x512/apps/wactorz.png"

COMMON=(-s dir -C "$STAGE" -n wactorz-desktop -v "$VERSION"
        --license "Apache-2.0"
        --description "Wactorz desktop (pywebview)"
        --url "https://github.com/waldiez/wactorz"
        --maintainer "Wactorz Contributors")

echo "==> [3/4] Building .deb"
fpm "${COMMON[@]}" -t deb \
    -d "python3 (>= 3.10)" -d "python3-gi" -d "python3-gi-cairo" \
    -d "gir1.2-webkit2gtk-4.1" \
    -p "$DIST/wactorz-desktop_${VERSION}_amd64.deb" .

echo "==> [4/4] Building .rpm"
fpm "${COMMON[@]}" -t rpm \
    -d "python3 >= 3.10" -d "python3-gobject" -d "webkit2gtk4.1" \
    -p "$DIST/wactorz-desktop-${VERSION}-1.x86_64.rpm" .

echo "==> Done:"
ls -1 "$DIST"/*.deb "$DIST"/*.rpm
