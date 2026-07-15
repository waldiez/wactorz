ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.20
FROM $BUILD_FROM

RUN apk add --no-cache \
    curl git jq gcc musl-dev linux-headers libffi-dev openssl-dev \
    mosquitto mosquitto-clients build-base cmake libjpeg-turbo-dev libpng-dev openblas-dev


RUN pip3 install --no-cache-dir opencv-python
RUN pip3 install --no-cache-dir matplotlib
RUN pip3 install --no-cache-dir torch==2.10.0 --index-url https://download.pytorch.org/whl/cpu

RUN wget https://download-r2.pytorch.org/whl/cpu/torchvision-0.25.0%2Bcpu-cp312-cp312-manylinux_2_28_aarch64.whl -O torchvision-0.25.0+cpu-cp312-cp312-linux_aarch64.whl
RUN pip3 install torchvision-0.25.0+cpu-cp312-cp312-linux_aarch64.whl && rm torchvision-0.25.0+cpu-cp312-cp312-linux_aarch64.whl

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
