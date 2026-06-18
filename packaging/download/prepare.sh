#!/usr/bin/env bash
# Assemble the download page for a test drop: collect the built installers under
# stable names, checksum them, and stamp the version into index.html.
#
# Gather the per-platform artifacts (.dmg / Setup.exe / .AppImage) into one dir
# first, then:
#   bash prepare.sh [ARTIFACTS_DIR] [OUT_DIR]      # defaults: ./incoming ./out
# Upload the contents of OUT_DIR to the VM.
set -euo pipefail
shopt -s nullglob

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${1:-$HERE/incoming}"
OUT="${2:-$HERE/out}"
REPO="$(cd "$HERE/../.." && pwd)"

VERSION="$(sed -n 's/.*__version__ *= *"\([^"]*\)".*/\1/p' "$REPO/wactorz/_version.py")"
[ -n "$VERSION" ] || { echo "could not read version from wactorz/_version.py" >&2; exit 1; }

mkdir -p "$OUT"

pick_first() {  # pick_first <stable-name> <glob>...  — copy the first match
  local name="$1"; shift
  local glob matches
  for glob in "$@"; do
    matches=("$SRC"/$glob)
    if [ "${#matches[@]}" -gt 0 ]; then
      cp "${matches[0]}" "$OUT/$name"
      echo "  $name  <-  $(basename "${matches[0]}")"
      return
    fi
  done
  echo "  (skip $name — none of: $*)"
}

echo "[prepare] version $VERSION; artifacts from $SRC"
pick_first "Wactorz-macos.dmg"         "*.dmg"
pick_first "Wactorz-Setup.exe"         "*Setup*.exe" "*.exe"
pick_first "Wactorz-x86_64.AppImage"   "*x86_64*.AppImage"
pick_first "Wactorz-aarch64.AppImage"  "*aarch64*.AppImage" "*arm64*.AppImage"

# Checksums over whatever was collected (skip if none).
cd "$OUT"
files=(Wactorz-*)
if [ "${#files[@]}" -gt 0 ]; then
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${files[@]}" > SHA256SUMS
  else
    sha256sum "${files[@]}" > SHA256SUMS
  fi
  echo "[prepare] SHA256SUMS written for ${#files[@]} file(s)"
else
  echo "[prepare] no installers found — page will have dead links"
fi

# Stamp the version into the page and bring along the hero screenshot.
sed "s/{{VERSION}}/$VERSION/g" "$HERE/index.html" > "$OUT/index.html"
cp "$REPO/frontend/public/screenshots/dashboard.png" "$OUT/dashboard.png" 2>/dev/null \
  || echo "[prepare] note: dashboard.png not found — hero image will be missing"
echo "[prepare] ready in $OUT — upload its contents to the VM."
