FROM python:3.12-slim

WORKDIR /app

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
RUN pip install --no-cache-dir ".[all]" \
    && rm -rf /app/wactorz /app/static /app/scripts /app/pyproject.toml /app/README.md

# Unprivileged runtime user. The entrypoint chowns the state directory before
# dropping to it — see docker-entrypoint.sh for why that cannot happen here.
RUN adduser --system --uid 1000 --group --home /home/wactorz wactorz \
    && mkdir -p /home/wactorz /app/state \
    && chown -R wactorz:wactorz /home/wactorz /app/state

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
ENV HOME=/home/wactorz \
    PIP_USER=1 \
    PYTHONUSERBASE=/app/state/.python \
    PIP_CACHE_DIR=/tmp/pip-cache

ENV INTERFACE=rest

EXPOSE 8000 8888

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["wactorz"]