ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:trixie
FROM $BUILD_FROM

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND="noninteractive"
ENV DEBCONF_NONINTERACTIVE_SEEN=true
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Unpinned on purpose: a pinned Debian version stops resolving once a security
# update replaces it. The upgrade brings in Debian's security updates since the
# base was published; a release build takes no cache, so it runs fresh each time.
# hadolint ignore=DL3008
RUN apt-get update && apt-get -y upgrade --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    curl git jq gcc linux-libc-dev libffi-dev libssl-dev python3-pip python3-dev python3-venv locales \
    pkg-config libcairo2-dev libgirepository1.0-dev \
    mosquitto mosquitto-clients build-essential cmake libjpeg62-turbo-dev libpng-dev libopenblas-dev \
    libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libxcb1 libx11-6 libgomp1 \
    && sed -i -e 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen \
    && locale-gen en_US.UTF-8 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LC_CTYPE=en_US.UTF-8 \
    TZ=Etc/UTC

# Python packages live in a virtual environment, not the system interpreter.
# The -dev packages above pull in Debian's own Python packages, and pip cannot
# upgrade one of those in place (it has no record of its files), so any install
# that needs a newer version of one fails. It is first on PATH, so `wactorz`,
# `python3` and `pip3` (the installer agent's `sys.executable -m pip` too) all
# resolve here.
ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# A layer of its own so its large download is cached apart from wactorz. The
# version is the one `wactorz[all,ml]` below accepts, which that install enforces.
# hadolint ignore=DL3013
RUN pip3 install --no-cache-dir ultralytics
# The wactorz tag, branch or sha to install. Required, with no default: a
# default would let a build quietly ship whatever that ref pointed at that day.
# The release workflow passes the tag, and build.yaml the release this add-on
# version belongs to.
ARG WACTORZ_REF
ARG BUILD_VERSION=local
RUN test -n "${WACTORZ_REF}" \
    || { echo "WACTORZ_REF is required: the wactorz tag, branch or sha to install" >&2; exit 1; }
RUN pip3 install --no-cache-dir \
    "wactorz[all,ml] @ git+https://github.com/waldiez/wactorz.git@${WACTORZ_REF}" \
    && test "$(command -v wactorz)" = "$VIRTUAL_ENV/bin/wactorz" \
    && python3 -c "import wactorz" \
    && echo "wactorz addon ${BUILD_VERSION} (ref ${WACTORZ_REF}) installed"

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
