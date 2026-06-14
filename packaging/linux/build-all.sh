#!/usr/bin/env bash
# Build the Linux desktop artifacts. Currently just the AppImage; Flatpak is
# built separately via flatpak-builder (see ../README.md) once scaffolded.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$HERE/build-appimage.sh"
