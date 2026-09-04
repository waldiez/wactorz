#!/bin/sh
# Fetch the model on first run, then serve it as an unprivileged account.
#
# The model is kept out of the image so it outlives a rebuild: a few hundred
# megabytes that never change. It is checked against its digest before it is
# unpacked -- it arrives over the network and is then loaded into this process.
set -eu

MODEL="${VOICE_MODEL:-sherpa-onnx-streaming-zipformer-en-20M-2023-02-17}"
MODEL_SHA256="${VOICE_MODEL_SHA256:-9c559283e8498d3fe95913c79ca1cb454bb26281ac2b102b41306c7d752765d9}"
DATA="${VOICE_DATA:-/data}"
DIR="${DATA}/${MODEL}"

if [ ! -d "${DIR}" ]; then
    echo "voice: fetching ${MODEL}"
    mkdir -p "${DATA}"
    curl -fsSL -o "${DATA}/model.tar.bz2" \
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${MODEL}.tar.bz2"
    if [ -n "${MODEL_SHA256}" ]; then
        # A model named by VOICE_MODEL without its digest cannot be checked; set
        # VOICE_MODEL_SHA256 to empty to say that is intended.
        echo "${MODEL_SHA256}  ${DATA}/model.tar.bz2" | sha256sum -c -
    fi
    tar xf "${DATA}/model.tar.bz2" -C "${DATA}"
    rm -f "${DATA}/model.tar.bz2"
fi

# The volume is owned by whoever created it, so it is handed over here rather
# than declared in the image, and privilege is dropped for the server itself.
if [ "$(id -u)" = "0" ]; then
    chown -R recogniser:recogniser "${DATA}"
    exec setpriv --reuid=recogniser --regid=recogniser --clear-groups "$0" "$@"
fi

# int8 for the two heavy parts: on a machine with no GPU this is what makes the
# words keep up with the speaker, and the decoder is small enough not to matter.
exec python /opt/streaming_server.py \
    --encoder "${DIR}/encoder-epoch-99-avg-1.int8.onnx" \
    --decoder "${DIR}/decoder-epoch-99-avg-1.onnx" \
    --joiner "${DIR}/joiner-epoch-99-avg-1.int8.onnx" \
    --tokens "${DIR}/tokens.txt" \
    --port "${VOICE_PORT:-6006}" \
    --doc-root /opt/web \
    "$@"
