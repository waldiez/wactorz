#!/usr/bin/env bash
# Run the Sinergym <-> MQTT <-> Fuseki bridge inside the EnergyPlus 25.1.0 / Sinergym 3.12.0 container.
#
# The bridge needs to reach the host's MQTT broker (:1883) and Fuseki (:3030).
# On Linux, host.docker.internal is NOT automatic — the --add-host line below maps
# it to the host gateway so the SETUP's `host.docker.internal` URLs resolve.
#
# The wactorz/sinergym/ dir (register_env.py, the bridge, anomaly_injector.py — copied
# in from maddpg_office) is bind-mounted at /work and used as the working dir, so the
# bridge can `from register_env import make_custom_env` and `from anomaly_injector ...`.
#
# Usage:
#   ./run-bridge.sh                       # clean deploy run (reproduces eval metrics)
#   ./run-bridge.sh --clean               # wipe the Fuseki dataset first, then run
#   ./run-bridge.sh --inject-anomalies --anomaly-seed 5   # with anomaly injection
# Any extra args are appended to the bridge command.
#
# Why --clean: the Fuseki dataset is persistent (TDB2) and every run is "episode 1",
# so without clearing, successive runs ACCUMULATE and collide on (step, zone) — the
# replay/queries then have to AVG-dedupe a mix of runs. Use --clean for a pristine
# dataset per run. (A future fix is to stamp each run with a unique id; see LOCAL_SETUP.)
set -euo pipefail

SGY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # wactorz/sinergym
IMAGE="wactorz-sinergym-bridge:3.12.0-ep25.1.0"
FUSEKI_HOST_URL="${FUSEKI_HOST_URL:-http://localhost:3030}"

# Strip --clean (the bridge doesn't understand it); wipe the dataset host-side first.
CLEAN=0; ARGS=()
for a in "$@"; do
  if [ "$a" = "--clean" ]; then CLEAN=1; else ARGS+=("$a"); fi
done
if [ "$CLEAN" = 1 ]; then
  echo "[clean] clearing Fuseki dataset 'sinergym' ($FUSEKI_HOST_URL)…"
  curl -fsS -u admin:admin -X POST "$FUSEKI_HOST_URL/sinergym/update" \
    --data-urlencode 'update=CLEAR ALL' >/dev/null && echo "[clean] dataset wiped."
fi

ZONES="Core_bottom,Core_mid,Core_top,\
Perimeter_bot_ZN_1,Perimeter_bot_ZN_2,Perimeter_bot_ZN_3,Perimeter_bot_ZN_4,\
Perimeter_mid_ZN_1,Perimeter_mid_ZN_2,Perimeter_mid_ZN_3,Perimeter_mid_ZN_4,\
Perimeter_top_ZN_1,Perimeter_top_ZN_2,Perimeter_top_ZN_3,Perimeter_top_ZN_4"

# anomaly_injector.py lives in state/maddpg_office/ on the host; expose it to the bridge.
INJECTOR_DIR="$(cd "$SGY_DIR/.." && pwd)/state/maddpg_office"

# NOTE: we must PREPEND to the container's existing PYTHONPATH, not replace it — the
# base image puts EnergyPlus's `pyenergyplus` module on PYTHONPATH, and clobbering it
# breaks `import sinergym`. So we run through a login shell and expand $PYTHONPATH there.
CMD=(python sinergym_bridge_anomalies.py
     --env officeMedium-multiagent --mode deploy --episodes 1
     --zones "$ZONES"
     --fuseki-url http://host.docker.internal:3030 --fuseki-dataset sinergym
     --fuseki-user admin --fuseki-password admin
     --broker host.docker.internal --port 1883
     "${ARGS[@]+"${ARGS[@]}"}")   # guard empty array under `set -u` (macOS bash 3.2)

# Use an interactive TTY only when we actually have one (so this also works backgrounded).
TTY=""; [ -t 0 ] && [ -t 1 ] && TTY="-it"
exec docker run --rm $TTY \
  --platform=linux/amd64 \
  --add-host=host.docker.internal:host-gateway \
  -v "$SGY_DIR":/work \
  -v "$INJECTOR_DIR":/injector:ro \
  -w /work \
  "$IMAGE" \
  bash -lc 'PYTHONPATH="/work:/injector:${PYTHONPATH}" exec "$@"' _ "${CMD[@]}"
