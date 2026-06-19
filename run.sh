#!/bin/bash
# Wactorz Unified Entry Point — starts the Python backend.

set -e

# Load .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

echo "Starting Wactorz (Python backend)..."

# Ensure virtualenv is used if available
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi
exec python3 -m wactorz "$@"
