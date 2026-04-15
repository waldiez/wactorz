#!/bin/bash
# Wactorz Unified Entry Point
# Controls whether to run the Rust or Python backend.

set -e

# Load .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# Default to python
DEFAULT_BACKEND="python"

BACKEND=${WACTORZ_BACKEND:-$DEFAULT_BACKEND}
RUST_BIN="./rust/target/release/wactorz-server"

echo "Starting Wactorz with ${BACKEND} backend..."

if [ "$BACKEND" = "rust" ]; then
    if [ ! -f "$RUST_BIN" ]; then
        echo "Error: Rust binary not found at $RUST_BIN"
        echo "Run 'make build-rust' first."
        exit 1
    fi
    exec "$RUST_BIN" "$@"
elif [ "$BACKEND" = "python" ]; then
    # Ensure virtualenv is used if available
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    fi
    exec python3 -m wactorz "$@"
else
    echo "Unknown backend: $BACKEND. Use 'rust' or 'python'."
    exit 1
fi
