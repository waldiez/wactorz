#!/bin/bash
# Wactorz entry point — starts the Python backend.

set -e

# Load .env if it exists.
# Sourced with allexport rather than `export $(grep ... | xargs)`: word-splitting
# the file breaks on the inline comments and quoted values that .env.template
# itself contains, and with `set -e` above that aborts the launch.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

echo "Starting Wactorz (Python backend)..."

# Ensure virtualenv is used if available
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi
exec python3 -m wactorz "$@"
