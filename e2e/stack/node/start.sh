#!/bin/sh
# Accept this run's key for the `node` user, then serve SSH in the foreground.
set -eu

install -d -m 0700 -o node -g node /home/node/.ssh
install -m 0600 -o node -g node /keys/id_ed25519.pub /home/node/.ssh/authorized_keys

# -e: log to the console, where `docker logs` and the suite can read it.
exec /usr/sbin/sshd -D -e
