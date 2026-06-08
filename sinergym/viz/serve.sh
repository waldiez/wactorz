#!/usr/bin/env bash
# One command to serve the Sinergym digital-twin viz.
#
#   ./serve.sh           build the web app (if needed) and serve everything on :8200
#   ./serve.sh --dev     hot-reload dev mode: Vite on :5180 + API server on :8200
#
# Live obs/action/anomaly data is read by the browser straight from mosquitto's
# WebSocket listener (:9001). Geometry + Fuseki history are served by server/app.py.
# Re-run the bridge (../docker/run-bridge.sh) to stream a run into the view.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(cd "$HERE/../.." && pwd)/.venv/bin/python"
[ -x "$PY" ] || PY=python3

cd "$HERE/web"
if [ "${1:-}" = "--dev" ]; then
  [ -d node_modules ] || bun install
  VIZ_PORT=8200 "$PY" "$HERE/server/app.py" &
  trap 'kill $! 2>/dev/null' EXIT
  exec bun run dev
fi

# production: build static once, then serve app + api from the python server
if [ ! -d dist ]; then
  [ -d node_modules ] || bun install
  bun run build
fi
echo "→ open http://localhost:8200"
exec env VIZ_PORT=8200 "$PY" "$HERE/server/app.py"
