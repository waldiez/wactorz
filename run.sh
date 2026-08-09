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

# Ensure virtualenv is used if available (POSIX layout: .venv/bin, Windows:
# .venv/Scripts). Built from $VIRTUAL_ENV (set by the activate script) rather
# than a PATH search: Windows venvs ship python.exe but no python3.exe, so
# `command -v python3` after activation would silently skip the venv and
# fall through to a system python3 on PATH.
PYTHON=""
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
    PYTHON="$VIRTUAL_ENV/bin/python"
elif [ -f ".venv/Scripts/activate" ]; then
    source .venv/Scripts/activate
    PYTHON="$VIRTUAL_ENV/Scripts/python.exe"
fi

if [ -z "$PYTHON" ]; then
    # Windows has no python3.exe by default; fall back to python.
    PYTHON=python3
    command -v python3 >/dev/null 2>&1 || PYTHON=python
fi

exec "$PYTHON" -m wactorz "$@"
