#!/bin/sh
# Smoke-test a built Wactorz image beside a real broker.
#
# Passes when both servers answer liveness and readiness with 200, which means
# the supervision tree started, `main` runs, the broker connection is up and the
# database answers; when the process no longer runs as root; and when no set-id
# binary is left in the image. The image binds wide, so it is started with an API
# key, as it refuses to start without one.
#
# An `ultra` image says so in WACTORZ_IMAGE_FLAVOUR, and is also asked for what
# it exists to carry: the vision packages imported, as the user the app runs as,
# and a package with no wheel built against the system libraries.
#
# Usage: scripts/image-smoke.sh <image> <mosquitto-image>
# `make image-smoke IMAGE=…` passes the broker image pinned in .github/tools/Dockerfile.
set -eu

# Under Git Bash on Windows, arguments that look like Unix paths (`/proc/1/status`,
# `find /`) are rewritten into Windows paths before docker sees them. These are
# paths inside the containers, so they must reach docker as written. No effect
# anywhere else.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

image=${1:?usage: image-smoke.sh <image> <mosquitto-image>}
broker_image=${2:?usage: image-smoke.sh <image> <mosquitto-image>}

# Generous: a first start on a slow runner creates the database and the broker
# connection, and the point is to catch a server that never gets there.
deadline_s=${SMOKE_TIMEOUT_S:-180}

run="wactorz-smoke-$$"

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        echo "--- the app's log" >&2
        docker logs "$run-app" 2>&1 | tail -n 80 >&2 || true
    fi
    docker rm -f "$run-app" "$run-broker" > /dev/null 2>&1 || true
    docker network rm "$run" > /dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT INT TERM

fail() {
    echo "smoke: $*" >&2
    exit 1
}

# A broker that takes anonymous clients, on a network only these two share. Its
# config is written inside the container rather than mounted from the host, so
# no host path has to be translated for docker (Docker Desktop on Windows).
docker network create "$run" > /dev/null
docker run -d --name "$run-broker" --network "$run" "$broker_image" \
    sh -c 'printf "listener 1883\nallow_anonymous true\n" > /tmp/smoke.conf && exec mosquitto -c /tmp/smoke.conf' \
    > /dev/null

# The app is started once the broker takes a client, so what is tested is the
# image rather than which of the two containers came up first.
waited=0
until docker exec "$run-broker" mosquitto_pub -h localhost -t smoke/ping -m up > /dev/null 2>&1; do
    [ "$waited" -lt 30 ] || fail "the broker did not accept a client within 30s"
    sleep 1
    waited=$((waited + 1))
done

# A fresh key per run: the image refuses to bind wide without one, and a key
# written into the script would be a credential in the repository.
key=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')

docker run -d --name "$run-app" --network "$run" \
    -e API_KEY="$key" \
    -e MQTT_HOST="$run-broker" -e MQTT_PORT=1883 \
    -e MQTT_USERNAME= -e MQTT_PASSWORD= \
    -e LLM_PROVIDER=none \
    "$image" > /dev/null

# The HTTP status of one probe, from inside the container: 000 while nothing listens.
status_of() {
    # curl prints 000 itself when it cannot connect, then exits non-zero; the
    # default covers a container that cannot run curl at all.
    code=$(docker exec "$run-app" curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
        "http://127.0.0.1:$1$2" 2> /dev/null) || true
    echo "${code:-000}"
}

probes="8000/health 8000/ready 8888/health 8888/ready"
elapsed=0
while :; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$run-app")" != true ]; then
        fail "the container exited before it was ready"
    fi
    pending=""
    for probe in $probes; do
        port=${probe%%/*}
        path=/${probe#*/}
        code=$(status_of "$port" "$path")
        [ "$code" = 200 ] || pending="$pending $port$path=$code"
    done
    [ -z "$pending" ] && break
    if [ "$elapsed" -ge "$deadline_s" ]; then
        echo "--- readiness, as the server reports it" >&2
        docker exec "$run-app" curl -s --max-time 5 http://127.0.0.1:8000/ready >&2 || true
        echo >&2
        fail "not ready after ${deadline_s}s:$pending"
    fi
    sleep 2
    elapsed=$((elapsed + 2))
done
echo "smoke: both servers live and ready after ${elapsed}s"

uid=$(docker exec "$run-app" awk '/^Uid:/ { print $2 }' /proc/1/status)
[ "$uid" != 0 ] || fail "the app runs as root: the entrypoint did not drop privileges"
echo "smoke: the app runs as uid $uid"

setid=$(docker exec "$run-app" find / -xdev -perm /6000 -type f 2> /dev/null || true)
[ -z "$setid" ] || fail "set-id binaries in the image: $setid"
echo "smoke: no set-id binaries"

# What the image passes on, and under which terms: Wactorz's own license and
# notice, in /app and in the installed package, and the list of everything else.
docker exec "$run-app" sh -c '
    test -s /app/LICENSE && test -s /app/NOTICE.md \
    && grep -q "^aiohttp [0-9].*: " /app/THIRD_PARTY.txt \
    && grep -q "^wactorz [0-9].*: Apache-2.0 (texts: LICENSE, NOTICE.md)$" /app/THIRD_PARTY.txt
' || fail "the image does not carry its license, its notice and the list of what it bundles"
echo "smoke: license, notice and $(docker exec "$run-app" sh -c 'wc -l < /app/THIRD_PARTY.txt') third-party packages listed"

flavour=$(docker exec "$run-app" printenv WACTORZ_IMAGE_FLAVOUR 2> /dev/null || true)
echo "smoke: the image says it is '${flavour:-default}'"
[ "$flavour" = ultra ] || exit 0

# As the app's user, which is who an agent's code runs as. Importing cv2 is what
# fails without libGL; a model run on an empty picture is what fails when
# PyTorch and Ultralytics disagree about each other.
docker exec -u "$uid" "$run-app" python -c '
import cv2, numpy, torch, torchvision, ultralytics
assert not torch.cuda.is_available(), "the CPU build of PyTorch is the one locked"
picture = numpy.zeros((64, 64, 3), dtype=numpy.uint8)
assert cv2.cvtColor(picture, cv2.COLOR_BGR2GRAY).shape == (64, 64)
assert torch.zeros(2, 2).sum().item() == 0
print("torch", torch.__version__, "ultralytics", ultralytics.__version__, "cv2", cv2.__version__)
' || fail "the ultra image cannot import what it carries"
echo "smoke: the vision packages import"

# What the Reachy Mini SDK needs at install time: PyGObject has no wheel, and is
# built against GObject introspection and cairo. The range is the SDK's own,
# since a later PyGObject wants a later introspection library than it does. Then
# GStreamer through it.
docker exec -u "$uid" "$run-app" sh -c '
    pkg-config --exists gobject-introspection-1.0 cairo \
    && pip install --quiet --no-cache-dir "PyGObject>=3.42.2,<=3.46.0" \
    && python -c "
import gi
gi.require_version(\"Gst\", \"1.0\")
from gi.repository import Gst
Gst.init(None)
assert Gst.ElementFactory.find(\"videotestsrc\") is not None
print(\"gstreamer\", Gst.version_string())
"' || fail "the ultra image cannot build PyGObject or reach GStreamer through it"
echo "smoke: PyGObject builds and reaches GStreamer"
