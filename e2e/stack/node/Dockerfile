# A machine to deploy a node to: Python, an SSH server, an ordinary user, and
# nothing of Wactorz. `/deploy` puts everything else there itself, over SSH, as
# it does on a Raspberry Pi.
#
# It has no systemd, so the deploy starts the node the way it does on a machine
# without one. A deploy to a real machine, with a unit, is the journey marked
# `real_node`.
FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81

# Unpinned on purpose: a pinned Debian version stops resolving once a security
# update replaces it.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server procps \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd \
    && useradd --create-home --shell /bin/bash node

# Keys only, and only for that user. The key it accepts is this run's, mounted
# at /keys and copied into place at start: sshd refuses an authorized_keys file
# that belongs to someone else, which a mounted one does.
RUN printf '%s\n' \
    'PasswordAuthentication no' \
    'PermitRootLogin no' \
    'AllowUsers node' \
    > /etc/ssh/sshd_config.d/e2e.conf

COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 22
CMD ["/start.sh"]
