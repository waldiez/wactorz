#!/bin/sh
# Smoke-test a built Wactorz image beside a real broker.
#
# Passes when both servers answer liveness and readiness with 200, which means
# the supervision tree started, `main` runs, the broker connection is up and the
# database answers; when the process no longer runs as root; and when no set-id
# binary is left in the image. The image binds wide, so it is started with an API
# key, as it refuses to start without one.
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
