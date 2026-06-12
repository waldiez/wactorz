ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
FROM $BUILD_FROM

RUN apk add --no-cache \
    curl git jq gcc musl-dev linux-headers libffi-dev openssl-dev \
    mosquitto mosquitto-clients

# WACTORZ_REF selects which git ref (branch/tag/sha) to pip-install. Defaults to
# main for production; override via build.yaml `args` or `--build-arg` to build/
# test a branch without hand-editing this file (see LOCAL_TESTING.md).
ARG WACTORZ_REF=main
# BUILD_VERSION is passed by HA Supervisor — referencing it here busts the
# Docker layer cache whenever the addon version is bumped in config.yaml.
ARG BUILD_VERSION=main
RUN pip3 install --no-cache-dir \
    "wactorz[all] @ git+https://github.com/waldiez/wactorz.git@${WACTORZ_REF}" \
    && echo "wactorz addon ${BUILD_VERSION} (ref ${WACTORZ_REF}) installed"

COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
