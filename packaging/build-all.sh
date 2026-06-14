#!/usr/bin/env bash
# Build all Linux desktop artifacts: AppImage + .deb + .rpm.
# Note: for portability these are best built in per-target environments
# (old-glibc container for the AppImage; Debian/Ubuntu for .deb; Fedora for
# .rpm). This convenience script runs them all on the current host.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

bash "$HERE/appimage/build-appimage.sh"
bash "$HERE/linux/build-deb-rpm.sh"
