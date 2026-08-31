"""TTS extension — server-side text-to-speech.

Where the speech is made depends on ``WACTORZ_TTS_URI``. Unset, it is made here
by edge-tts (``pip install wactorz[tts]``). Set, it is made by the service that
address names: an HTTP endpoint, which needs nothing beyond what the server
already has, or a Wyoming synthesiser over ``tcp://``, which is spoken to with
``wactorz[tts]``, which carries it.

``WACTORZ_TTS`` says who does the speaking. ``server`` makes the audio here and
sends it to the browser to play; ``browser`` leaves it to the browser's own
voice, so the text never goes anywhere; ``host`` makes it here and plays it
through this machine's own speakers, answering into a room rather than a page;
``off`` says nothing at all.

If no backend can be reached the extension still loads: the routes answer 503
and ``public_config()`` reports ``available: false``, so the browser falls back
to the Web Speech API rather than going quiet.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from aiohttp import web

from ... import config
from . import remote, speaker, spoken

logger = logging.getLogger(__name__)


class TTSState:  # pylint: disable=too-few-public-methods
    """TTS capability and voice cache, mutated once at startup, read-only after."""

    def __init__(self) -> None:
        self.available: bool = False
        # https://github.com/rany2/edge-tts/blob/master/src/edge_tts/constants.py#L9
        self.default_voice: str = "en-US-EmmaMultilingualNeural"
        self.voices: list[dict[str, str]] | None = None


# ---------------------------------------------------------------------------
# State (single instance, mutated once at startup)
# ---------------------------------------------------------------------------
_tts_state = TTSState()

try:
    import edge_tts
    from edge_tts import constants

    _tts_state.available = True
    try:
        _tts_state.default_voice = constants.DEFAULT_VOICE
    except Exception:  # pylint: disable=broad-exception-caught
        pass
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Extension contract
# ---------------------------------------------------------------------------
def setup(app: web.Application) -> None:
    """Register TTS routes and warm-start the voice cache."""
    app.router.add_get("/api/tts/voices", tts_voices_handler)
    app.router.add_post("/api/tts", tts_handler)
    app.on_startup.append(_warm_tts_voices)
    _warn_if_the_room_will_stay_quiet()


def _warn_if_the_room_will_stay_quiet() -> None:
    """Say at startup what would otherwise be found a turn at a time.

    ``host`` answers out loud and nowhere else, so a deployment that cannot do
    it is silent rather than broken-looking: no error reaches the person who
    asked, because their answer arrived on screen exactly as it should. Said
    once here rather than as a warning after every turn.
    """
    if config.TTS_MODE != "host":
        return
    if not speaker.available():
        logger.warning(
            "[tts] WACTORZ_TTS=host, but this machine has no sound device to speak "
            "through — pip install 'wactorz[host]', or choose another branch"
        )
    # Only a Wyoming synthesiser is certain to answer in something playable
    # without a decoder: it speaks in raw samples, which are given a WAV header
    # on the way back. The in-process one answers in MP3, and an HTTP endpoint
    # answers in whatever it likes -- so neither can be assumed safe here.
    if not speaker.can_play("audio/mpeg") and not remote.is_wyoming_uri(remote.service_uri()):
        logger.warning(
            "[tts] WACTORZ_TTS=host can only play WAV without a decoder, and what this "
            "deployment synthesises may not be — pip install 'wactorz[host]'"
        )


def synthesiser_available() -> bool:
    """Whether this deployment will make speech here for the browser to play.

    False for every branch but ``server``: ``off`` wants silence, and ``browser``
    wants its own voice, so answering yes would send the text somewhere the
    chosen branch says it must not go.
    """
    if config.TTS_MODE != "server":
        return False
    uri = remote.service_uri()
    # Taken at its word rather than probed: the browser asks this on every page
    # load, and a synthesiser is allowed to be asleep until something needs
    # speaking. What it is spoken to with still has to be installed, though --
    # an HTTP one needs nothing beyond what the server already has, and a
    # Wyoming one needs the optional dependency.
    if remote.is_http_uri(uri):
        return True
    if remote.is_wyoming_uri(uri):
        return remote.WYOMING
    return _tts_state.available


def worth_saying(text: str) -> str:
    """Trim `text` to what is worth reading aloud, and write it as it is said.

    Code read out loud is noise, and a synthesiser charged by the character has
    no business with a thousand-line answer. Mirrors the same trimming in
    `TTSManager.ts`, which applies it to the branches the browser speaks.

    Measurements are spelled out here rather than there: a browser's own voice
    and a hosted service both normalise what they are given, and a self-hosted
    synthesiser reads the characters -- so "21 °C" comes out with no degrees in
    it, and "5 m/s" comes out with a "slash".
    """
    return spoken.speakable(re.sub(r"```[\s\S]*?```", "code block", text)[:300])


async def make_speech(text: str, asked_for: str = "") -> remote.Speech:
    """Turn `text` into audio, by whichever synthesiser this deployment names."""
    configured = os.environ.get("TTS_VOICE", "").strip()
    uri = remote.service_uri()
    if remote.names_a_service(uri):
        # No fallback to the default below: that name belongs to the synthesiser
        # this process would have used, and a service asked for a voice it has
        # never heard of refuses the whole request. Sending none is how it is
        # asked for the one it is configured with.
        return await remote.synthesise(uri, text, asked_for or configured)

    voice = asked_for or configured or _tts_state.default_voice
    # Bounded like every other synthesiser: this one is reached over the network
    # too, and it is on the path of a whole turn now that the machine can answer
    # out loud -- a stream that stops arriving would hold that turn open.
    return await asyncio.wait_for(_made_here(text, voice), remote.TIMEOUT)


async def _made_here(text: str, voice: str) -> remote.Speech:
    """Synthesise in this process, with the optional dependency that does it."""
    communicate = edge_tts.Communicate(text, voice)
    chunks: list[bytes] = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            data = chunk.get("data")
            if data:
                chunks.append(data)
    return remote.Speech(audio=b"".join(chunks), content_type="audio/mpeg")


async def speak_here(text: str) -> None:
    """Say `text` through this machine's own speakers.

    For ``WACTORZ_TTS=host``, where the answer comes out of the room rather than
    a page. Failures are logged and swallowed: the reply has already reached
    whoever asked for it, and a silent machine is a worse answer than a quiet
    one but not a reason to fail the turn.
    """
    if not text.strip():
        return
    spoken = worth_saying(text)
    try:
        made = await make_speech(spoken)
        await speaker.play(made.audio)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[tts] Could not speak here: %s", exc)


def stop_speaking() -> None:
    """Stop anything being said out loud, for a turn someone cancelled."""
    speaker.silence()


def _reported_voice() -> str:
    """The voice this deployment speaks in, as far as the browser needs to know.

    Empty for a named service: its voices are its own, and naming the one this
    process would have used describes a synthesiser that is not being asked.
    """
    configured = os.getenv("TTS_VOICE", "").strip()
    if remote.names_a_service(remote.service_uri()):
        return configured
    # Stripped and `or`-ed rather than given as a default argument: a default
    # applies only when the name is absent, and `.env.template` tells you to
    # leave this one empty for it. `load_dotenv` then supplies "", which reached
    # the synthesiser and was refused, so the documented setup produced no speech
    # at all. Whitespace goes the same way, because a `.env` line keeps trailing
    # spaces more often than anyone means it to.
    return configured or _tts_state.default_voice


def _why_silent() -> str:
    """Why this deployment will not speak, in terms of what to change.

    Named exactly rather than listed: the reasons ask for different things, and
    a message offering all of them points at the wrong one every time but once.
    The address itself is never quoted -- the browser is told what to fix, not
    where this deployment's network keeps its services.
    """
    if config.TTS_MODE == "host":
        return "this deployment speaks through its own speakers (WACTORZ_TTS=host)"
    if config.TTS_MODE != "server":
        return f"this deployment does not speak here (WACTORZ_TTS={config.TTS_MODE})"
    uri = remote.service_uri()
    if remote.is_wyoming_uri(uri) and not remote.WYOMING:
        return "WACTORZ_TTS_URI names a Wyoming synthesiser — pip install 'wactorz[tts]'"
    return "no synthesiser: set WACTORZ_TTS_URI, or pip install 'wactorz[tts]'"


def public_config(_app: web.Application) -> dict[str, Any]:
    """Non-secret TTS config for the browser."""
    # The address is deliberately absent, as it is for recognition: the browser
    # never speaks to the synthesiser, and where it lives is a fact about the
    # network this deployment sits on.
    return {
        "mode": config.TTS_MODE,
        "available": synthesiser_available(),
        "voice": _reported_voice(),
    }


# ---------------------------------------------------------------------------
# Voice cache (warmed at startup)
# ---------------------------------------------------------------------------
async def _warm_tts_voices(_app: web.Application | None = None) -> None:
    """Load edge-tts voice list once at startup and cache it."""
    if not _tts_state.available:
        return
    try:
        voices = await edge_tts.list_voices()
        _tts_state.voices = [
            {"name": v["ShortName"], "locale": v["Locale"], "gender": v["Gender"]}
            for v in sorted(voices, key=lambda v: v["ShortName"])
        ]
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("[tts] Failed to load voice list")
        _tts_state.voices = []


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def tts_voices_handler(_request: web.Request) -> web.Response:
    """GET /api/tts/voices — the voices this deployment can speak in.

    A named service answers with nothing rather than a guess: its voices are its
    own, and the browser reads an empty list as "no choice to make here" and
    speaks in whatever the service is configured for.
    """
    if remote.names_a_service(remote.service_uri()):
        return web.json_response([])
    if _tts_state.voices is None:
        await _warm_tts_voices()
    return web.json_response(_tts_state.voices or [])


async def tts_handler(request: web.Request) -> web.Response:
    """POST /api/tts {"text": ..., "voice": ...} — speak the text.

    Where the speech is made follows ``WACTORZ_TTS_URI``; the audio comes back
    under whatever type that produced. 503 when this deployment will not speak,
    so the browser falls back to the Web Speech API transparently.

    POST rather than GET because this route does work: each call synthesizes
    audio through an outbound service. A GET is assumed to be a read, and any
    page can fire one cross-origin with no `Origin` header at all — `<img
    src="…/api/tts?text=…">` is enough — which is exactly the assumption an
    Origin check relies on. A POST carries a body, so it cannot be triggered
    that way.
    """
    if not synthesiser_available():
        return web.Response(status=503, text=_why_silent())

    try:
        body = await request.json()
    except Exception:
        return web.Response(status=400, text="expected a JSON body")
    if not isinstance(body, dict):
        return web.Response(status=400, text="expected a JSON object")

    text = str(body.get("text") or "").strip()
    if not text:
        return web.Response(status=400, text="text is required")

    text = worth_saying(text)

    asked_for = str(body.get("voice") or "").strip()

    try:
        spoken = await make_speech(text, asked_for)
        return web.Response(
            body=spoken.audio,
            content_type=spoken.content_type,
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Logged in full, answered in summary: the exception comes from a
        # third-party service and can carry a URL or a request detail.
        logger.warning("[tts] Synthesis failed: %s", exc)
        return web.Response(status=500, text="Speech synthesis failed")
