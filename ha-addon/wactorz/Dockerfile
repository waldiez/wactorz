ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.14-alpine3.24
FROM $BUILD_FROM

# Unpinned on purpose: an Alpine package version pinned here stops resolving once
# the base image's repository moves on, which breaks the build on its own. The
# upgrade brings in Alpine's security updates since the base was published; a
# release build takes no cache, so it runs fresh each time.
# hadolint ignore=DL3018
RUN apk upgrade --no-cache && apk add --no-cache \
    curl git jq gcc musl-dev linux-headers libffi-dev openssl-dev \
    mosquitto mosquitto-clients

# The wactorz tag, branch or sha to install. Required, with no default: a
# default would let a build quietly ship whatever that ref pointed at that day.
# The release workflow passes the tag, and build.yaml the release this add-on
# version belongs to.
ARG WACTORZ_REF
ARG BUILD_VERSION=local
RUN test -n "${WACTORZ_REF}" \
    || { echo "WACTORZ_REF is required: the wactorz tag, branch or sha to install" >&2; exit 1; }
RUN pip3 install --no-cache-dir \
    "wactorz[all] @ git+https://github.com/waldiez/wactorz.git@${WACTORZ_REF}" \
    && python3 -c "import wactorz" \
    && echo "wactorz addon ${BUILD_VERSION} (ref ${WACTORZ_REF}) installed"

# The user Wactorz runs as. run.sh starts as root, for what needs it, and hands
# over to this one. It owns nothing in the image: the code it runs is not its
# to rewrite, and what agents install at runtime goes under /data.
RUN addgroup -S -g 1000 wactorz \
    && adduser -S -D -H -u 1000 -G wactorz -h /data/state/home -s /sbin/nologin wactorz

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
