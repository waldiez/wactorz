#!/bin/sh
# Run the tests that need a real broker (tests/broker) against mosquittos
# started for the run and removed after it.
#
# One takes anonymous clients and has no access list, for the contract between
# main and a node. The other two load the access list the server generates
# (tests/broker/acl_fixture.py writes them), for what that list lets each
# account do: one where the server has an account and anonymous clients are
# refused, as compose and the add-ons run it, and one where the server connects
# with no account. All are published on free ports of this machine's loopback
# only, so nothing else on the network reaches them and two runs side by side
# do not collide.
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
locked="wactorz-test-broker-locked-$$"
unnamed="wactorz-test-broker-unnamed-$$"
work=$(mktemp -d)

cleanup() {
    status=$?
    docker rm -f "$name" "$locked" "$unnamed" > /dev/null 2>&1 || true
    rm -rf "$work"
    exit "$status"
}
trap cleanup EXIT INT TERM

# Wait until the broker in container $1 takes a client; $2 is how to log in.
wait_for_broker() {
    waited=0
    # shellcheck disable=SC2086  # $2 is a list of arguments, split on purpose
    until docker exec "$1" mosquitto_pub -h localhost $2 -t test/ping -m up > /dev/null 2>&1; do
        [ "$waited" -lt 30 ] || { echo "test-broker: $1 did not accept a client within 30s" >&2; exit 1; }
        sleep 1
        waited=$((waited + 1))
    done
}

# `docker port` prints `127.0.0.1:<port>`; the port is what follows the last colon.
port_of() {
    found=$(docker port "$1" 1883/tcp | head -n 1 | sed 's/.*://')
    [ -n "$found" ] || { echo "test-broker: could not read the port of $1" >&2; exit 1; }
    echo "$found"
}

docker run -d --name "$name" -p 127.0.0.1::1883 "$image" \
    sh -c 'printf "listener 1883\nallow_anonymous true\n" > /tmp/test.conf && exec mosquitto -c /tmp/test.conf' \
    > /dev/null

wait_for_broker "$name" ""

# Start a broker in container $1 with the node accounts and access list in
# directory $2. $3 is whether it takes clients with no account, and $4 the
# other accounts it knows, each with the fixture's password. The files go in
# through `docker exec` rather than a mount, so no host path has to be
# translated for docker; the broker waits for them before it starts.
password="broker-test-password"
start_with_access_list() {
    docker run -d --name "$1" -p 127.0.0.1::1883 \
        -e BROKER_PASSWORD="$password" -e ALLOW_ANONYMOUS="$3" -e BROKER_ACCOUNTS="$4" "$image" sh -c '
        mkdir -p /fixture
        while [ ! -f /fixture/ready ]; do sleep 0.2; done
        : > /fixture/passwd
        for account in $BROKER_ACCOUNTS; do
            mosquitto_passwd -b /fixture/passwd "$account" "$BROKER_PASSWORD"
        done
        cat /fixture/node_passwd >> /fixture/passwd
        chown mosquitto:mosquitto /fixture/passwd /fixture/acl
        chmod 0600 /fixture/passwd /fixture/acl
        printf "listener 1883\nallow_anonymous %s\npassword_file /fixture/passwd\nacl_file /fixture/acl\nsys_interval 1\n" \
            "$ALLOW_ANONYMOUS" > /fixture/broker.conf
        exec mosquitto -c /fixture/broker.conf' > /dev/null
    # The container is up but may not have made /fixture yet.
    waited=0
    until docker exec "$1" test -d /fixture 2> /dev/null; do
        [ "$waited" -lt 30 ] || { echo "test-broker: the container of $1 did not start" >&2; exit 1; }
        sleep 1
        waited=$((waited + 1))
    done
    docker exec -i "$1" sh -c 'cat > /fixture/node_passwd' < "$2/node_passwd"
    docker exec -i "$1" sh -c 'cat > /fixture/acl' < "$2/acl"
    docker exec "$1" touch /fixture/ready
}

# The node accounts and access lists are generated the way the server generates
# them, under a state directory the tests are given too, so they derive the same
# node passwords.
WACTORZ_STATE_DIR="$work/state" "$python" -m tests.broker.acl_fixture "$work/broker"

start_with_access_list "$locked" "$work/broker" false "wactorz homeassistant outsider"
start_with_access_list "$unnamed" "$work/broker/unnamed" true ""
wait_for_broker "$locked" "-u wactorz -P $password"
wait_for_broker "$unnamed" ""

WACTORZ_TEST_BROKER="127.0.0.1:$(port_of "$name")" \
    WACTORZ_TEST_ACL_BROKER="127.0.0.1:$(port_of "$locked")" \
    WACTORZ_TEST_ACL_UNNAMED_BROKER="127.0.0.1:$(port_of "$unnamed")" \
    WACTORZ_TEST_ACL_STATE="$work/state" \
    "$python" -m pytest tests/broker -p no:cacheprovider "$@"
