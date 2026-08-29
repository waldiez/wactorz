# A sherpa-onnx streaming recogniser, for WACTORZ_STT_URI=ws://...
#
# Built here rather than pulled: the project publishes no server image, and the
# one piece that is needed -- the websocket server from its Python examples -- is
# not part of the wheel. Both are pinned to the same release, and the script is
# checked against its digest: a tag can be moved, a digest cannot.
FROM python:3.14-slim

ARG SHERPA_VERSION=1.13.6
ARG SERVER_SHA256=3f74d3c0f57724b1ed5fd7a637b37b31b6b5e396a80d6f7b2ecc8b65b1a65752
ARG HTTP_SHA256=a78a7df73d44b7c8a6793a6454c10b948d245460d9f1ec69612eb2785e6901a0

# bzip2 to unpack the model, curl to fetch it, setpriv to drop privilege.
RUN apt-get update \
    && apt-get install -y --no-install-recommends bzip2 ca-certificates curl util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "sherpa-onnx==${SHERPA_VERSION}" numpy websockets

# http_server is imported by the one above, from the same directory.
RUN base="https://raw.githubusercontent.com/k2-fsa/sherpa-onnx/v${SHERPA_VERSION}/python-api-examples" \
    && curl -fsSL -o /opt/streaming_server.py "${base}/streaming_server.py" \
    && curl -fsSL -o /opt/http_server.py "${base}/http_server.py" \
    && echo "${SERVER_SHA256}  /opt/streaming_server.py" | sha256sum -c - \
    && echo "${HTTP_SHA256}  /opt/http_server.py" | sha256sum -c - \
    && mkdir -p /opt/web

# The demo page that ships with the server is not served here -- nothing uses it,
# and it would be an unauthenticated page on the recogniser's port. Its loader
# reads every file in its list at startup, so the files are created empty rather
# than pulling third-party scripts into the image to satisfy it.
COPY prepare-web.py /opt/prepare-web.py
RUN python /opt/prepare-web.py

# The account the recogniser runs as. It owns nothing it can write except the
# model directory, which the entrypoint hands over before dropping into it.
RUN groupadd --system --gid 10001 recogniser \
    && useradd --system --uid 10001 --gid 10001 \
       --create-home --home-dir /home/recogniser recogniser

# The server writes its own log directory beside wherever it is started from,
# so it starts somewhere the unprivileged account can write.
WORKDIR /home/recogniser

COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod 0755 /opt/entrypoint.sh

EXPOSE 6006
ENTRYPOINT ["/opt/entrypoint.sh"]
