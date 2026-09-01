"""STT extension — server-side speech-to-text via a Wyoming ASR service.

Optional dependency: ``pip install wactorz[stt]`` (installs wyoming). If it is
not installed the extension still loads -- the route answers 503 and
``public_config()`` reports ``available: false``, so a deployment configured for
a branch it cannot serve offers no microphone rather than one whose every
recording fails.

Wyoming is a network protocol, which is what makes the recogniser's location a
setting rather than a deployment: the service named by ``WACTORZ_STT_URI`` may
run beside this process, on the Home Assistant host, or on a machine bought for
the purpose, and nothing here changes between those.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time
import wave
from collections.abc import AsyncIterator
from typing import Any

from aiohttp import BodyPartReader, web

from ... import config
from ...monitoring.log_redaction import redact
from ..tts import speaker
from . import listener, streaming

logger = logging.getLogger(__name__)

#: Samples per Wyoming audio message. Small enough that a long clip does not
#: arrive as one oversized frame, large enough not to spend the transfer on
#: per-message overhead.
CHUNK_BYTES = 4096


class TooLarge(Exception):
    """The clip outgrew the limit while being read."""


#: Largest clip accepted, counted while reading rather than trusting a header.
#: A minute of 16-bit 16 kHz mono is about 2 MB, so this bounds an utterance
#: generously while still refusing a stream that never ends.
MAX_AUDIO_BYTES = 10 * 1024 * 1024

#: How long to wait for the recogniser to accept a connection. A service that
#: is not there fails at once; this bounds one that accepts slowly.
CONNECT_TIMEOUT = 10.0

#: How long one clip may take end to end. Deliberately generous: Whisper on a CPU
#: loads its model on first use and can take most of a minute to answer, and a
#: limit that cuts off slow-but-healthy work only trades one failure for another.
TRANSCRIBE_TIMEOUT = 120.0

#: Where the recogniser listens. The default is the port Wyoming Whisper uses,
#: on this host, which is what a Home Assistant add-on install already provides.
DEFAULT_URI = "tcp://localhost:10300"


class STTState:  # pylint: disable=too-few-public-methods
    """Recognition capability, decided once at import and read-only after."""

    def __init__(self) -> None:
        self.available: bool = False


_stt_state = STTState()

try:  # optional dependency — the module must import without a recogniser installed
    from wyoming.asr import Transcribe, Transcript
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.client import AsyncClient

    _stt_state.available = True
except ImportError:
    pass


def service_uri() -> str:
    """The configured recogniser, or the local default.

    Stripped and ``or``-ed rather than passed as a default: a default applies
    only when the name is absent, and an empty value in a ``.env`` is a name
    that is present.
    """
    return os.getenv("WACTORZ_STT_URI", "").strip() or DEFAULT_URI


#: Branches a client can act on. The rest are accepted by configuration so a
#: deployment can be set up ahead of them, and say so rather than going quiet.
SERVED_MODES = ("server",)


def setup(app: web.Application) -> None:
    """Register the recognition routes, and account for a branch nothing serves."""
    app.router.add_post("/api/stt", stt_handler)
    app.router.add_post("/api/stt/listen", listen_handler)

    # Read through the module rather than bound at import, so the value is the
    # one in force when the app is built.
    mode = config.STT_MODE
    if mode == "host" and not listener.available():
        logger.warning(
            "[stt] WACTORZ_STT=host, but this machine has no microphone to listen "
            "through — pip install 'wactorz[host]', or choose another branch"
        )
    if mode not in SERVED_MODES and mode != "host" and mode != "off":
        # Otherwise a valid setting produces an interface with no microphone and
        # nothing anywhere saying why, which reads as a broken install.
        logger.warning(
            "[stt] WACTORZ_STT=%s is configured, but no microphone is offered for it yet. "
            "Set WACTORZ_STT=%s for speech recognition.",
            mode,
            SERVED_MODES[0],
        )


def recogniser_reachable() -> bool:
    """Whether this deployment can transcribe at all.

    Depends on which recogniser it was pointed at. A streaming one is spoken to
    over a plain websocket, which needs nothing beyond what the server already
    has; a Wyoming one needs the optional dependency. Answering with the Wyoming
    answer for both would hide a working microphone from a deployment that has
    one.
    """
    if streaming.is_streaming_uri(service_uri()):
        return True
    return _stt_state.available


def public_config(_app: web.Application) -> dict[str, Any]:
    """Non-secret recognition config for the browser."""
    # The URI is deliberately absent: the browser never speaks to the recogniser,
    # and an address is a fact about the network this deployment sits on. Whether
    # it streams is a capability rather than an address, and the composer has to
    # know before it opens the microphone: the two branches capture differently
    # from the first frame, so this cannot be discovered by trying one.
    return {
        "available": recogniser_reachable(),
        "live": streaming.is_streaming_uri(service_uri()),
    }


def _pcm_from_wav(raw: bytes) -> tuple[bytes, int, int, int]:
    """Frames, rate, sample width and channel count from a WAV clip.

    WAV rather than what a browser records by default: decoding WebM or Ogg
    needs a codec this process does not have, and requiring one would make a
    system package the difference between the feature working and not. A caller
    that has audio in another container converts before sending.
    """
    with wave.open(io.BytesIO(raw), "rb") as handle:
        return (
            handle.readframes(handle.getnframes()),
            handle.getframerate(),
            handle.getsampwidth(),
            handle.getnchannels(),
        )


async def _exchange(
    client: AsyncClient, frames: bytes, rate: int, width: int, channels: int
) -> str:
    """Drive one recognition exchange on a connected client.

    Separate from `transcribe` so it can be handed to `wait_for` as a whole:
    `asyncio.timeout` would read better but arrived in 3.11, and this package
    still supports 3.10.
    """
    await client.write_event(Transcribe().event())
    await client.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
    for begin in range(0, len(frames), CHUNK_BYTES):
        await client.write_event(
            AudioChunk(
                rate=rate,
                width=width,
                channels=channels,
                audio=frames[begin : begin + CHUNK_BYTES],
            ).event()
        )
    await client.write_event(AudioStop().event())

    while True:
        event = await client.read_event()
        if event is None:
            raise RuntimeError("the recogniser closed the connection without transcribing")
        if Transcript.is_type(event.type):
            # Stripped: recognisers conventionally lead with a space, and the
            # caller appends this to whatever the composer already holds.
            return Transcript.from_event(event).text.strip()


async def transcribe(raw: bytes, uri: str | None = None) -> str:
    """Send one clip to the recogniser and return what it heard."""
    frames, rate, width, channels = _pcm_from_wav(raw)
    client = AsyncClient.from_uri(uri or service_uri())

    # Bounded separately: refusing to connect and answering slowly are different
    # failures, and a service loading a model deserves far longer than one that
    # is not listening at all.
    try:
        await asyncio.wait_for(client.connect(), CONNECT_TIMEOUT)
        return await asyncio.wait_for(
            _exchange(client, frames, rate, width, channels), TRANSCRIBE_TIMEOUT
        )
    finally:
        # Safe on every path, including one where the connection never opened:
        # with no writer to close this does nothing, rather than raising over
        # the error that brought us here.
        await client.disconnect()


async def hear(clip: bytes) -> str:
    """Read a recorded clip, through whichever recogniser this deployment names.

    The scheme decides, as it does everywhere else: a Wyoming service takes the
    clip whole, and a streaming one is fed the same audio in frames and asked
    what it settled on. Without this the branch that owns a microphone would work
    against one kind of recogniser and raise against the other, including the one
    `infra/voice/stt/` builds.
    """
    uri = service_uri()
    if not streaming.is_streaming_uri(uri):
        return await transcribe(clip)

    frames, rate, _width, _channels = _pcm_from_wav(clip)
    settled: dict[int, str] = {}

    async def audio() -> AsyncIterator[bytes]:
        step = max(1, int(rate * streaming.FRAME_SECONDS)) * 2
        for start in range(0, len(frames), step):
            yield streaming.as_float32(frames[start : start + step])

    def keep(reading: streaming.Partial) -> None:
        # Each reading replaces its segment rather than adding to it, so the last
        # one for a segment is what was heard.
        settled[reading.segment] = reading.text

    await streaming.transcribe_stream(uri, audio(), keep)
    return " ".join(settled[k] for k in sorted(settled) if settled[k]).strip()


async def listen_handler(request: web.Request) -> web.Response:
    """POST /api/stt/listen — hear the room, and act on what was said.

    The branch that owns a microphone has no button to press, so something has to
    ask it to listen. Answers with the text, and routes it as though it had been
    typed: an ``@mention`` reaches the agent it names and anything else reaches
    main, which is one path to reason about rather than two.
    """
    if config.STT_MODE != "host":
        return web.json_response(
            {"error": f"this deployment does not listen here (WACTORZ_STT={config.STT_MODE})"},
            status=503,
        )
    if not listener.available():
        return web.json_response(
            {"error": "no microphone on this machine — pip install 'wactorz[host]'"}, status=503
        )
    if speaker.is_speaking():
        # The same room, one device answering into what the other is listening
        # to. Recording now takes the reply as the next question, and a machine
        # that asks itself does not stop, or stop spending.
        return web.json_response({"text": "", "heard": False, "speaking": True})

    try:
        clip = await listener.listen()
    except listener.NoMicrophone as exc:
        return web.json_response({"error": str(exc)}, status=503)
    except asyncio.TimeoutError:
        # Spelled this way on purpose: the two are one object from 3.11 and
        # separate on 3.10, which this still supports, and only this spelling
        # catches what `wait_for` raises on both.
        # The microphone stopped delivering. That is the same thing to whoever
        # asked as one that never opened, and is not this request's failure.
        return web.json_response({"error": "the microphone stopped delivering audio"}, status=503)

    if not clip:
        # Silence is an answer: nobody spoke, and there is nothing to route.
        return web.json_response({"text": "", "heard": False})

    try:
        said = (await hear(clip)).strip()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[stt] Could not transcribe what the room said: %s", exc)
        return web.json_response({"error": "transcription failed"}, status=502)

    if said:
        await _route_as_typed(request, said)
    return web.json_response({"text": said, "heard": bool(said)})


async def _route_as_typed(request: web.Request, said: str) -> None:
    """Send what was heard down the path a typed message takes.

    Typed means typed: the turn is written to the chat log and shown to whatever
    browsers are open, exactly as one from the composer would be. A microphone
    that listens to a room and leaves no record of what it heard or what was
    answered is the wrong thing to build, and the answer would otherwise reach
    nobody at all unless the same machine also speaks.

    Imported here rather than at module scope: the web layer builds on the
    extensions, so an extension reaching back to it at import time closes a
    circle that neither side can start.
    """
    from ...web import chat, runtime, ws

    addressed = "main" if said.startswith("/") else chat.parse_mention(said)[0]
    _remember(runtime, "user", said, addressed)
    await ws.broadcast({"type": "chat", "content": said, "from": "user", "to": addressed})

    streamed: list[str] = []

    async def whole(text: str | None) -> None:
        """Keep and show one complete reply, as the socket would have."""
        if not text:
            return
        _remember(runtime, "assistant", text, addressed)
        await ws.broadcast({"type": "chat", "content": text, "from": addressed, "to": "user"})

    async def piece(chunk: str) -> None:
        """Hold one piece of a reply that is still arriving."""
        streamed.append(chunk)

    async def ended(*_args: object, **_kwargs: object) -> None:
        """The reply is complete: record and show it once."""
        text, streamed[:] = "".join(streamed), []
        await whole(text)

    # Gathered rather than written as it arrives: an agent that streams sends its
    # answer in dozens of pieces, and treating each as a reply would put dozens of
    # rows in the log, dozens of bubbles on the page, and -- where this machine
    # also speaks -- dozens of separate utterances for one sentence.
    async def safely() -> None:
        """Answer the turn, and say so if answering it fails.

        A task nobody awaits swallows what it raised: the reply never arrives,
        nothing reaches the log or the page, and the only trace is a warning from
        the collector long afterwards.
        """
        try:
            await chat.route_chat(said, whole, stream_fn=piece, stream_end_fn=ended)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[stt] The turn heard from the room could not be answered: %s", exc)

    chat.track_chat_task(asyncio.create_task(safely()))


def _remember(runtime: Any, role: str, content: str, agent_name: str) -> None:
    """Write one turn to the chat log, or carry on without it."""
    if runtime.db is None or not content:
        return
    try:
        # Redacted like every other turn that reaches the log: someone can say a
        # credential aloud as easily as type one, and this is a room microphone.
        runtime.db.write_chat_log(
            ts=time.time(), agent_name=agent_name, role=role, content=redact(content)
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # The turn still happened and was still answered; losing the record of
        # it is not a reason to fail the request that heard it.
        logger.warning("[stt] Could not record what the room said: %s", exc)


async def _read_audio(request: web.Request) -> bytes:
    """Read the uploaded clip, refusing one that outgrows the limit.

    Read in chunks against a running total rather than in one call: the size a
    request declares is not the size it sends, and the point of a limit is to
    stop reading rather than to describe what was already read.
    """
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        # A nested multipart reads back as a reader rather than a part, and it
        # carries no bytes of its own to collect.
        if not isinstance(part, BodyPartReader) or part.name != "audio":
            continue
        collected = bytearray()
        while True:
            chunk = await part.read_chunk()
            if not chunk:
                break
            collected += chunk
            if len(collected) > MAX_AUDIO_BYTES:
                raise TooLarge
        return bytes(collected)
    raise LookupError("no audio part")


async def stt_handler(request: web.Request) -> web.Response:
    """POST /api/stt with an ``audio`` part — transcribe it.

    Returns ``{"text": ...}``. 503 when wyoming is not installed, so a browser
    can tell "this deployment does not recognise speech" from "it tried and
    failed".
    """
    if not _stt_state.available:
        return web.json_response(
            {"error": "wyoming not installed — pip install 'wactorz[stt]'"}, status=503
        )

    # request.multipart() asserts rather than raising for a body that is not
    # multipart at all, which would surface as a 500 for a caller error.
    if not (request.content_type or "").startswith("multipart/"):
        return web.json_response({"error": "expected a multipart body"}, status=415)

    try:
        raw = await _read_audio(request)
    except TooLarge:
        return web.json_response({"error": f"larger than {MAX_AUDIO_BYTES} bytes"}, status=413)
    except LookupError:
        return web.json_response({"error": "expected an audio part"}, status=400)

    if not raw:
        return web.json_response({"error": "the audio part was empty"}, status=400)

    try:
        text = await transcribe(raw)
    except wave.Error:
        return web.json_response({"error": "expected a WAV clip"}, status=415)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # The recogniser is a separate service that can be absent, starting, or
        # broken, and none of those are this request's fault.
        logger.warning("[stt] %s did not transcribe: %s", service_uri(), exc)
        return web.json_response({"error": "the recogniser did not answer"}, status=502)

    return web.json_response({"text": text})
