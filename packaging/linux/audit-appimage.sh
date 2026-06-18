#!/usr/bin/env bash
# Audit an AppImage (or an extracted AppDir) for libraries it references
# (DT_NEEDED) but does NOT bundle — i.e. everything it relies on the host to
# provide. That's the portability risk surface: anything here beyond the
# universal base libs can crash on a host that happens to lack it (this is how
# the libwebp.so.6 crash would have been caught before shipping).
#
# Run on a Linux box with readelf (binutils), matching the AppImage's arch:
#   bash packaging/linux/audit-appimage.sh dist/Wactorz-aarch64.AppImage
set -euo pipefail

TARGET="$(readlink -f "${1:?usage: audit-appimage.sh <AppImage|AppDir>}")"
command -v readelf >/dev/null || { echo "need readelf (apt install binutils)" >&2; exit 1; }

if [ -d "$TARGET" ]; then
    ROOT="$TARGET"
else
    TMP="$(mktemp -d)"
    ( cd "$TMP" && "$TARGET" --appimage-extract >/dev/null )   # no FUSE needed
    ROOT="$TMP/squashfs-root"
fi

needed="$(find "$ROOT" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) -print0 \
    | while IFS= read -r -d '' f; do readelf -d "$f" 2>/dev/null || true; done \
    | sed -nE 's/.*\(NEEDED\).*\[(.+)\]/\1/p' | sort -u)"
shipped="$(find "$ROOT" \( -name '*.so' -o -name '*.so.*' \) -printf '%f\n' | sort -u)"

echo "== NEEDED but NOT bundled (expected from the host) =="
comm -23 <(printf '%s\n' "$needed") <(printf '%s\n' "$shipped")
cat <<'EOF'

Base libs are normally safe to rely on:
  libc/libm/libdl/libpthread/librt/libresolv, ld-linux*, libstdc++/libgcc_s,
  libGL*/libEGL*/libgbm/libdrm, libX11/libxcb*/libxkbcommon*, libwayland*,
  libglib*/libgobject*, libz/libssl/libcrypto.
Anything ELSE in the list is a portability risk — confirm it exists on the
leanest host you target, or add its package to Dockerfile.appimage so it bundles.
EOF
