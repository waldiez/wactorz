ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
FROM $BUILD_FROM

RUN apk add --no-cache \
    curl git jq gcc musl-dev linux-headers libffi-dev openssl-dev \
    mosquitto mosquitto-clients \
    ffmpeg

# Git ref of wactorz to install.
ARG WACTORZ_REF=main
ARG BUILD_VERSION=main
RUN pip3 install --no-cache-dir \
    "wactorz[all] @ git+https://github.com/waldiez/wactorz.git@${WACTORZ_REF}" \
    && echo "wactorz addon ${BUILD_VERSION} (ref ${WACTORZ_REF}) installed"

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
