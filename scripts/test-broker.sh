#!/bin/sh
# Run the tests that need a real broker (tests/broker) against a mosquitto
# started for the run and removed after it.
#
# The broker takes anonymous clients and is published on a free port of this
# machine's loopback only, so nothing else on the network reaches it and two
# runs side by side do not collide.
#
# Usage: scripts/test-broker.sh <mosquitto-image> <python> [pytest arguments]
# `make test-broker` passes the image pinned in .github/tools/Dockerfile and the
# project's interpreter.
set -eu

# Under Git Bash on Windows, arguments that look like Unix paths are rewritten
# into Windows paths before docker sees them; the path below is inside the
# container. No effect anywhere else.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

image=${1:?usage: test-broker.sh <mosquitto-image> <python> [pytest arguments]}
python=${2:?usage: test-broker.sh <mosquitto-image> <python> [pytest arguments]}
shift 2

name="wactorz-test-broker-$$"

cleanup() {
    status=$?
    docker rm -f "$name" > /dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT INT TERM

docker run -d --name "$name" -p 127.0.0.1::1883 "$image" \
    sh -c 'printf "listener 1883\nallow_anonymous true\n" > /tmp/test.conf && exec mosquitto -c /tmp/test.conf' \
    > /dev/null

waited=0
until docker exec "$name" mosquitto_pub -h localhost -t test/ping -m up > /dev/null 2>&1; do
    [ "$waited" -lt 30 ] || { echo "test-broker: the broker did not accept a client within 30s" >&2; exit 1; }
    sleep 1
    waited=$((waited + 1))
done

# `docker port` prints `127.0.0.1:<port>`; the port is what follows the last colon.
port=$(docker port "$name" 1883/tcp | head -n 1 | sed 's/.*://')
[ -n "$port" ] || { echo "test-broker: could not read the broker's port" >&2; exit 1; }

WACTORZ_TEST_BROKER="127.0.0.1:$port" "$python" -m pytest tests/broker -p no:cacheprovider "$@"
