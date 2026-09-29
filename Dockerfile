# The base is pinned by digest in a literal FROM line, so Dependabot proposes its
# updates (.github/dependabot.yml) and every build starts from the same bytes.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS app

WORKDIR /app

# Unpinned on purpose: a pinned Debian version stops resolving once a security
# update replaces it, and the `runtime` stage below upgrades it regardless.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY README.md .
COPY wactorz/ ./wactorz/
COPY static/ ./static/
COPY scripts/ ./scripts/

# Installed into the root-owned system site-packages on purpose: the runtime user
# must not be able to rewrite the code it is running. The build inputs are removed
# in the same layer, or they stay in the image as a second, shadowing copy of the
# package (~22MB) that `python` picks up ahead of the installed one.
# This checkout is what is installed, and its dependencies are the ranges in
# pyproject.toml, which a library must keep; there is nothing here to pin.
# hadolint ignore=DL3013
RUN pip install --no-cache-dir ".[all]" \
    && rm -rf /app/wactorz /app/static /app/scripts /app/pyproject.toml /app/README.md

# Unprivileged runtime user. The entrypoint chowns the state directory before
# dropping to it — see docker-entrypoint.sh for why that cannot happen here.
RUN adduser --system --uid 1000 --group --home /home/wactorz wactorz \
    && mkdir -p /home/wactorz /app/state \
    && chown -R wactorz:wactorz /home/wactorz /app/state \
    # su, mount, passwd and friends are unreachable from the runtime user —
    # the entrypoint drops privilege with --no-new-privs. Clearing the bits
    # anyway means the image does not depend on that flag being remembered.
    && { find / -xdev \( -perm -4000 -o -perm -2000 \) -type f -exec chmod -s {} + 2>/dev/null || true; }

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Set after the build-time install so it only affects runtime ones. Agents install
# packages at runtime (the spawn config's `install` list, via InstallerAgent) and
# the runtime user cannot write system site-packages, so PIP_USER redirects those —
# pip honours it without the caller passing --user. The target sits inside the state
# directory rather than ~/.local for three reasons: it is the one location already
# guaranteed writable, so the root filesystem can be mounted read-only; packages
# then survive container recreation instead of dying with the writable layer; and
# what an agent installed stays clearly separate from what the image shipped.
#
# HOME points inside the state directory, not at a home in the image layer: the
# root filesystem is (should be) read-only at runtime, and code that writes under 
# `~` would otherwise fail. The Google integrations are the live example — they keep 
# OAuth tokens at `~/.wactorz/` and refresh them in place, so an image-layer home 
# means Calendar and Gmail break at the first token expiry rather than at startup.
# Putting it in the state mount makes those writes work *and* persist.
ENV HOME=/app/state/home \
    PIP_USER=1 \
    PYTHONUSERBASE=/app/state/.python \
    PIP_CACHE_DIR=/tmp/pip-cache

ENV INTERFACE=rest

# A published port cannot reach a process bound to the container's own loopback,
# so the image binds wide. It deliberately does *not* set WACTORZ_EXPOSED_OK:
# that flag means "the only way in is already authenticated", and an image
# cannot know whether its ports were published to a loopback mapping or to the
# world. So `docker run -p 8888:8888 …` refuses to start until the operator
# says which — `-e API_KEY=…` or `-e WACTORZ_EXPOSED_OK=1` — and the refusal
# names both. Loud beats a container that starts and serves nothing.
ENV WACTORZ_BIND_HOST=0.0.0.0

EXPOSE 8000 8888

# Liveness only: 200 means the server is accepting requests, not that MQTT, a
# provider or any agent is healthy. A deeper probe would turn a broker blip into
# a restart loop. PORT is honoured because it is configurable (and defaults to
# 8080 under DEV_MODE); the start period covers agents and providers coming up.
# Shell form on purpose: the exec form would not expand ${PORT:-8000}.
# hadolint ignore=DL3025
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/health" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["wactorz"]

# Debian's security updates since the base image was published, which its digest
# alone only ever falls further behind. CI and release builds never take this
# stage from the layer cache (`no-cache-filters: runtime`), so every image gets
# the updates of the day it is built. An upgrade can bring set-id bits back with
# the packages it replaces, so they are cleared again.
FROM app AS runtime
RUN apt-get update && apt-get -y upgrade --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && { find / -xdev \( -perm -4000 -o -perm -2000 \) -type f -exec chmod -s {} + 2>/dev/null || true; }