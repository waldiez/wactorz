ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-debian:trixie
FROM $BUILD_FROM

ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND="noninteractive"
ENV DEBCONF_NONINTERACTIVE_SEEN=true
ENV PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git jq gcc linux-libc-dev libffi-dev libssl-dev python3-pip python3-dev locales \
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

RUN pip3 install --no-cache-dir ultralytics
# Git ref of wactorz to install.
ARG WACTORZ_REF=main
ARG BUILD_VERSION=main
RUN pip3 install --no-cache-dir \
    "wactorz[all,ml] @ git+https://github.com/waldiez/wactorz.git@${WACTORZ_REF}" \
    && echo "wactorz addon ${BUILD_VERSION} (ref ${WACTORZ_REF}) installed"

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
