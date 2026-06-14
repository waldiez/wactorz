#!/bin/bash
# Wactorz entry point — starts the Python backend.

set -e

# Load .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

# WACTORZ_BACKEND is kept for backward compatibility; only "python" is supported.
BACKEND=${WACTORZ_BACKEND:-python}
if [ "$BACKEND" != "python" ]; then
    echo "Unknown backend: $BACKEND. Only the Python backend is supported." >&2
    exit 1
fi

echo "Starting Wactorz (Python backend)..."

# Ensure virtualenv is used if available
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi
exec python3 -m wactorz "$@"
