"""STT extension: the dashboard microphone transcribed on the server.

The browser records, converts the recording to 16 kHz mono WAV and posts it to
``POST /api/stt``; the answer is the transcript. Recognition uses the same
configuration as Reachy's own microphone (``REACHY_STT_BACKEND`` and its key:
Deepgram by default, or faster-whisper / whisper locally), so one setting
decides where speech goes whichever microphone heard it.

``public_config()`` reports whether that recognizer can run, so the dashboard
offers the server option only when it would work and otherwise falls back to
the browser's own speech recognition.
"""

import asyncio
import logging
from typing import Any

from aiohttp import BodyPartReader, web

from wactorz.catalogue_agents import reachy_stt

logger = logging.getLogger(__name__)

#: Largest upload accepted. A minute of 16 kHz mono 16-bit audio is under 2 MB;
#: the margin is for browsers that cannot resample and send 48 kHz instead.
MAX_AUDIO_BYTES = 12 * 1024 * 1024

#: Longest a local recognizer may run on one clip before the request gives up.
#: Hosted ones are bounded by their own `REACHY_STT_TIMEOUT_S`. Generous, since
#: the first use of a local model may include loading it.
LOCAL_TIMEOUT_S = 180.0


def setup(app: web.Application) -> None:
    """Register the transcription route."""
    app.router.add_post("/api/stt", stt_handler)


def public_config(_app: web.Application) -> dict[str, Any]:
    """Non-secret STT status for the browser: whether the server can transcribe.

    `problem` names a missing package or environment variable, never a value.
    """
    problem = reachy_stt.configuration_problem()
    try:
        backend: str | None = reachy_stt.STTConfig.resolve().backend
    except ValueError:
        backend = None
    return {"available": problem is None, "backend": backend, "problem": problem}


def _is_wav(data: bytes) -> bool:
    return len(data) > 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


async def _read_audio(request: web.Request) -> bytes:
    """The uploaded clip, from a multipart `audio` field or the raw body."""
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        while True:
            part = await reader.next()
            if part is None:
                return b""
            if isinstance(part, BodyPartReader) and part.name == "audio":
                return bytes(await part.read(decode=False))
    return await request.read()


async def stt_handler(request: web.Request) -> web.Response:
    """POST /api/stt: WAV audio in, `{"text", "backend", "language"}` out.

    503 when the configured recognizer cannot run (with the reason), 413 for an
    oversized clip, 400 for anything that is not WAV, 502 when the recognizer
    fails, 504 when it does not answer in time.
    """
    problem = reachy_stt.configuration_problem()
    if problem:
        return web.json_response({"error": problem}, status=503)
    if request.content_length is not None and request.content_length > MAX_AUDIO_BYTES:
        return web.json_response({"error": "audio clip is too large"}, status=413)

    try:
        audio = await _read_audio(request)
    except Exception:
        return web.json_response({"error": "could not read the uploaded audio"}, status=400)
    if len(audio) > MAX_AUDIO_BYTES:
        return web.json_response({"error": "audio clip is too large"}, status=413)
    if not _is_wav(audio):
        return web.json_response({"error": "expected 16-bit PCM WAV audio"}, status=400)

    config = reachy_stt.STTConfig.resolve()
    limit = config.timeout_s + 5.0 if config.backend in ("deepgram", "openai") else LOCAL_TIMEOUT_S
    try:
        result = await asyncio.wait_for(reachy_stt.transcribe_wav(audio), limit)
    except asyncio.TimeoutError:
        return web.json_response({"error": "speech recognition timed out"}, status=504)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Logged in full, answered in summary: the error comes from a third-party
        # service and can carry a URL or a request detail.
        logger.warning("[stt] transcription failed: %s", exc)
        return web.json_response({"error": "speech recognition failed"}, status=502)
    return web.json_response(
        {"text": result.text, "backend": result.backend, "language": result.language},
        headers={"Cache-Control": "no-store"},
    )
