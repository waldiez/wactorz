#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Wactorz — native build helper
#
# Run this ON THE TARGET HOST if Rust is installed.
# Produces a native binary — no cross-compilation, no Docker build, no QEMU.
#
# Usage:
#   bash scripts/build-native.sh
#
# Result: ./wactorz  (stripped release binary, ready to run)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

BOLD=$'\e[1m'; RESET=$'\e[0m'; GREEN=$'\e[32m'; CYAN=$'\e[36m'

echo ""
echo "${BOLD}▶ Wactorz — native Rust build${RESET}"
echo ""

# Check Rust
if ! command -v cargo >/dev/null 2>&1; then
    echo "  Rust/cargo not found."
    echo "  Install with: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    exit 1
fi
echo "  ${CYAN}Cargo: $(cargo --version)${RESET}"
echo "  ${CYAN}Target: $(rustc -vV | grep host | awk '{print $2}')${RESET}"
echo ""

echo "${BOLD}▶ Building release binary…${RESET}"
cd rust
cargo build --release --bin wactorz
cd ..

# Copy binary to project root for easy access
cp rust/target/release/wactorz ./wactorz
echo ""
echo "${GREEN}✓ Built: ./wactorz  ($(du -sh wactorz | cut -f1))${RESET}"
echo ""
echo "  Run:  source .env && ./wactorz --no-cli"
echo "  Or:   bash scripts/deploy-native.sh"
