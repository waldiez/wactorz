"""Synthesis somewhere other than this process, named by ``WACTORZ_TTS_URI``.

Two kinds, told apart by the scheme, the same way the recogniser's address works:

* ``tcp://``  a Wyoming synthesiser -- ``wyoming-piper`` and anything else that
  speaks the protocol. Self-hosted, so the words never leave the network.
* ``http://`` / ``https://`` an HTTP endpoint that takes text and answers with
  audio, which is the shape most hosted services already have.

An HTTP endpoint's audio is passed on as it arrives, under the type it gave: the
browser decodes by sniffing, so an MP3 plays like a WAV and re-encoding would
cost quality for nothing. A Wyoming synthesiser answers in raw samples with no
container at all, which no browser can decode, so those are given the WAV header
that describes them.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import wave
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

try:  # optional dependency — the module must import without a synthesiser installed
    from wyoming.audio import AudioChunk, AudioStop
    from wyoming.client import AsyncClient
    from wyoming.error import Error
    from wyoming.tts import Synthesize, SynthesizeVoice

    WYOMING = True
except ImportError:
    WYOMING = False

#: How long to wait for a synthesiser before giving up on it. Generous, because
#: a self-hosted voice on a cold model can take seconds for its first sentence.
TIMEOUT = 60.0

#: How long to wait for a connection to close before abandoning it.
CLOSE_TIMEOUT = 5.0

#: What a Wyoming synthesiser answers with, and what an HTTP one usually does.
WAV = "audio/wav"

#: The most audio one sentence may answer with. The text is already capped
#: before it gets here, so anything past this is a service misbehaving, and
#: reading it to the end would spend this process's memory on it.
MAX_AUDIO = 10 * 1024 * 1024


@dataclass(frozen=True)
class Speech:
    """Synthesised audio, and what it is."""

    audio: bytes
    content_type: str


def service_uri() -> str:
    """The configured synthesiser, or empty when none is."""
    return os.getenv("WACTORZ_TTS_URI", "").strip()


def is_wyoming_uri(uri: str) -> bool:
    """Whether `uri` names a Wyoming synthesiser."""
    return urlparse(uri).scheme == "tcp"


def is_http_uri(uri: str) -> bool:
    """Whether `uri` names an HTTP synthesiser."""
    return urlparse(uri).scheme in {"http", "https"}


def names_a_service(uri: str) -> bool:
    """Whether `uri` names a synthesiser this module can drive."""
    return is_wyoming_uri(uri) or is_http_uri(uri)


async def synthesise(uri: str, text: str, voice: str) -> Speech:
    """Speak `text`, in whichever voice the backend at `uri` understands."""
    if is_wyoming_uri(uri):
        return await _wyoming(uri, text, voice)
    return await _http(uri, text, voice)


async def _wyoming(uri: str, text: str, voice: str) -> Speech:
    """Drive a Wyoming synthesiser and package what it says.

    Spoken through the protocol's own library rather than by writing its frames
    here: an audio event carries its samples in a second length-prefixed section
    that a hand-rolled reader is liable to mistake for the first.
    """
    if not WYOMING:
        raise RuntimeError("wyoming not installed — pip install 'wactorz[tts]'")

    request = Synthesize(text=text)
    if voice:
        request.voice = SynthesizeVoice(name=voice)

    client = AsyncClient.from_uri(uri)
    await asyncio.wait_for(client.connect(), TIMEOUT)
    try:
        await asyncio.wait_for(client.write_event(request.event()), TIMEOUT)
        return await asyncio.wait_for(_collect(client), TIMEOUT)
    finally:
        # Bounded too, and its failures dropped: closing waits on a peer that
        # may be the very thing that stopped answering, and the caller is owed
        # the original failure rather than one from tidying up after it.
        with contextlib.suppress(asyncio.TimeoutError, ConnectionError, OSError):
            await asyncio.wait_for(client.disconnect(), CLOSE_TIMEOUT)


async def _collect(client: AsyncClient) -> Speech:
    """Read events until the synthesiser stops, keeping the samples."""
    chunks: list[bytes] = []
    rate, width, channels = 22050, 2, 1
    while True:
        event = await client.read_event()
        if event is None:
            # The connection ended without a stop. Whatever arrived is all there
            # is, and an empty answer is a failure rather than a silent reply.
            break
        if Error.is_type(event.type):
            failure = Error.from_event(event)
            raise RuntimeError(f"synthesiser refused: {failure.text or failure.code}")
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            rate, width, channels = chunk.rate, chunk.width, chunk.channels
            chunks.append(chunk.audio)
        elif AudioStop.is_type(event.type):
            break
    # Joined first: a synthesiser that answers with empty chunks has said
    # nothing, and a container built around nothing plays as a moment of silence
    # rather than reporting that it failed.
    pcm = b"".join(chunks)
    if not pcm:
        raise RuntimeError("synthesiser returned no audio")
    return Speech(audio=_as_wav(pcm, rate, width, channels), content_type=WAV)


def _as_wav(pcm: bytes, rate: int, width: int, channels: int) -> bytes:
    """Wrap raw samples in the header that says what they are.

    The protocol carries samples and their shape in separate places, so what
    arrives cannot be played by anything that expects a file. The browser needs
    a container to decode at all, and this is the cheapest one that fits.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()


async def _http(uri: str, text: str, voice: str) -> Speech:
    """POST text to an HTTP synthesiser and take the audio it answers with."""
    body: dict[str, str] = {"input": text, "text": text}
    if voice:
        body["voice"] = voice
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.post(uri, json=body) as resp,
    ):
        if resp.status >= 400:
            raise RuntimeError(f"synthesiser answered {resp.status}")
        content_type = resp.headers.get("Content-Type", WAV)
        # Read to a ceiling rather than to the end: this is an address an
        # operator gave, but a wrong one can answer with a stream that never
        # stops, and that must cost a refusal rather than this process.
        audio = await resp.content.read(MAX_AUDIO + 1)
    if len(audio) > MAX_AUDIO:
        raise RuntimeError("synthesiser answered with more audio than one turn can need")
    return Speech(audio=audio, content_type=content_type)
