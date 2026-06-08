#!/usr/bin/env bash
# Run the Sinergym <-> MQTT <-> Fuseki bridge inside the 24.1.0 / 3.11.0 container.
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
#   ./run-bridge.sh --inject-anomalies --anomaly-seed 5   # with anomaly injection
# Any extra args are appended to the bridge command.
set -euo pipefail

SGY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # wactorz/sinergym
IMAGE="wactorz-sinergym-bridge:3.11.0-ep24.1.0"

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
     "$@")

exec docker run --rm -it \
  --add-host=host.docker.internal:host-gateway \
  -v "$SGY_DIR":/work \
  -v "$INJECTOR_DIR":/injector:ro \
  -w /work \
  "$IMAGE" \
  bash -lc 'PYTHONPATH="/work:/injector:${PYTHONPATH}" exec "$@"' _ "${CMD[@]}"
