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
# the app keeps PID 1 and receives signals directly.
set -e

: "${WACTORZ_STATE_DIR:=/app/state}"

mkdir -p "$WACTORZ_STATE_DIR"
chown -R wactorz:wactorz "$WACTORZ_STATE_DIR"

exec setpriv --reuid=wactorz --regid=wactorz --clear-groups --no-new-privs "$@"
