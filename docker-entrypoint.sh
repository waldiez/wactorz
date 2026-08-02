#!/bin/sh
# Drop to an unprivileged user, after making the state directory writable.
#
# The chown cannot move to build time: WACTORZ_STATE_DIR is usually a host bind
# mount, and the host uid that owns it is unknowable when the image is built.
# So this runs as root only long enough to fix ownership, then hands off.
#
# setpriv (util-linux, already in the base image) rather than gosu: same
# privilege drop — real, effective AND saved uid, verified unable to regain
# root — with no extra package, plus --no-new-privs, which stops any setuid
# binary in the image from elevating afterwards. It execs rather than forks, so
# no shell lingers between the supervisor and the app; under compose an init
# process owns PID 1 and forwards signals (see `init: true` there).
set -e

: "${WACTORZ_STATE_DIR:=/app/state}"

mkdir -p "$WACTORZ_STATE_DIR"

# HOME lives under the state directory (see the Dockerfile) because the root
# filesystem is read-only. Attempted here so a fresh, root-owned volume gets it
# before the chown below, but non-fatal: with DAC_OVERRIDE dropped, root cannot
# create a subdirectory in a state directory owned by the host user, and it does
# not need to — the application creates it on demand, as the user that owns it.
mkdir -p "${HOME:-$WACTORZ_STATE_DIR/home}" 2>/dev/null || true

# Best-effort, deliberately not fatal. On Linux this is what makes the mount
# writable and it succeeds. On Docker Desktop (macOS/Windows) the bind mount goes
# through a virtualising filesystem that presents files as accessible regardless
# and may refuse the chown outright — there it is unnecessary, so failing hard
# would break the container on the platforms that never needed it. If it does
# fail somewhere it matters, the app reports an unwritable state directory next.
if ! chown -R wactorz:wactorz "$WACTORZ_STATE_DIR" 2>/dev/null; then
    echo "[entrypoint] could not chown $WACTORZ_STATE_DIR — continuing" >&2
fi

exec setpriv --reuid=wactorz --regid=wactorz --clear-groups --no-new-privs "$@"
