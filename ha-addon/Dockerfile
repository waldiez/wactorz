ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
FROM $BUILD_FROM

RUN apk add --no-cache \
    curl git jq gcc musl-dev linux-headers libffi-dev openssl-dev \
    mosquitto mosquitto-clients

# BUILD_VERSION is passed by HA Supervisor — referencing it here busts the
# Docker layer cache whenever the addon version is bumped in config.yaml.
ARG BUILD_VERSION=main
RUN pip3 install --no-cache-dir "wactorz[all] @ git+https://github.com/waldiez/wactorz.git@main" \
    && echo "wactorz addon ${BUILD_VERSION} installed"

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
