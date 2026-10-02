# Which image to build. `default` is the one published as `wactorz:<version>`:
# Wactorz and its integrations, and nothing an agent that only talks to APIs does
# not need. `ultra` is published as `wactorz:<version>-ultra`: the same, plus
# what vision models and the Reachy Mini SDK need -- PyTorch, Ultralytics and
# OpenCV installed, and the system libraries and build tools without which
# those, or a package an agent installs later, cannot be imported or built.
ARG FLAVOUR=default

# uv, at build time only: it turns uv.lock into the list the install below
# checks every package against, and is never copied into the image. Pinned by
# digest like the base; a test keeps it on the uv version CI installs.
FROM ghcr.io/astral-sh/uv:0.12.21@sha256:a7aed3216253ee804de3e2d8afa5073baa1a177335345d43845cd4165e43b711 AS uv

# Each base is pinned by digest in a literal FROM line, so Dependabot proposes its
# updates (.github/dependabot.yml) and every build starts from the same bytes.
# A build makes only the one its FLAVOUR names.
FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d AS base-default

# Unpinned on purpose, here and below: a pinned Debian version stops resolving
# once a security update replaces it, and the `runtime` stage upgrades it
# regardless.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# One Python release behind the default image: the Reachy Mini SDK needs a
# PyGObject that does not build on a newer one.
FROM python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81 AS base-ultra

# Three kinds of package. What OpenCV and PyTorch load when they are imported
# (libGL, GLib, the X libraries, OpenMP). What builds a package that ships no
# wheel, PyGObject among them, when an agent installs one while Wactorz runs: a
# compiler, pkg-config and the headers. And GStreamer with its introspection
# data, which the Reachy Mini SDK reaches its robot through; ffmpeg is what
# that agent makes its speech louder with.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxcb1 libx11-6 libgomp1 \
    build-essential cmake git pkg-config \
    libffi-dev libssl-dev libcairo2-dev libgirepository1.0-dev \
    libjpeg62-turbo-dev libpng-dev libopenblas-dev \
    gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
    gstreamer1.0-nice gstreamer1.0-libav \
    gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 gir1.2-gst-plugins-bad-1.0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# hadolint ignore=DL3006
FROM base-${FLAVOUR} AS app

# Repeated here: a stage sees only the arguments it declares.
ARG FLAVOUR

WORKDIR /app

COPY pyproject.toml README.md LICENSE NOTICE.md uv.lock ./
COPY wactorz/ ./wactorz/
COPY static/ ./static/
COPY scripts/ ./scripts/

# The dependencies are the versions uv.lock pins, each checked against its hash,
# so the image holds what CI tested and a package altered on its way here fails
# the build. uv only writes that list; pip installs it, and stays in the image
# for the packages agents install at runtime. Wactorz itself goes in last, with
# nothing left to resolve.
#
# Installed into the root-owned system site-packages on purpose: the runtime user
# must not be able to rewrite the code it is running. The build inputs are removed
# in the same layer, or they stay in the image as a second, shadowing copy of the
# package that `python` picks up ahead of the installed one.
#
# The `ultra` image adds the `ml` and `vision` extras. Their PyTorch is locked to
# PyTorch's own CPU build, which is not on PyPI, so pip is told where that index
# is; every file from it is checked against the lockfile's hash like the rest.
#
#
# An image passes on every package installed in it, so it says what they are:
# THIRD_PARTY.txt lists each with its version and license, beside Wactorz's own
# LICENSE and NOTICE, which stay in /app. The texts of the others are where pip
# put them, in each package's .dist-info folder.
#
# `.` is this checkout, and its dependencies are pinned by hash the line before.
# `$extras` and `$indexes` are unquoted on purpose: each is a list of arguments,
# or none.
# hadolint ignore=DL3013,SC2086
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    case "$FLAVOUR" in \
        default) extras="--extra all"; indexes="" ;; \
        ultra) extras="--extra all --extra ml --extra vision"; \
               indexes="--extra-index-url https://download.pytorch.org/whl/cpu" ;; \
        *) echo "FLAVOUR is '$FLAVOUR': it is 'default' or 'ultra'" >&2; exit 1 ;; \
    esac \
    && uv export --quiet --frozen --no-emit-project $extras --format requirements.txt \
        --output-file /tmp/requirements.txt \
    && pip install --no-cache-dir --require-hashes $indexes -r /tmp/requirements.txt \
    && pip install --no-cache-dir --no-deps . \
    && python scripts/third_party.py > /app/THIRD_PARTY.txt \
    && rm -rf /tmp/requirements.txt /app/wactorz /app/static /app/scripts \
        /app/pyproject.toml /app/README.md /app/uv.lock

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

# Which of the two this is, for whoever asks from inside it.
ENV WACTORZ_IMAGE_FLAVOUR=${FLAVOUR}

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
