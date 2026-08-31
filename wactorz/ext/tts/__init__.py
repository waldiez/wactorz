"""TTS extension — server-side text-to-speech.

Where the speech is made depends on ``WACTORZ_TTS_URI``. Unset, it is made here
by edge-tts (``pip install wactorz[tts]``). Set, it is made by the service that
address names: an HTTP endpoint, which needs nothing beyond what the server
already has, or a Wyoming synthesiser over ``tcp://``, which is spoken to with
``wactorz[voice]`` -- the same dependency the recogniser uses.

If no backend can be reached the extension still loads: the routes answer 503
and ``public_config()`` reports ``available: false``, so the browser falls back
to the Web Speech API rather than going quiet.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from aiohttp import web

from ... import config
from . import remote

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


def _why_silent() -> str:
    """Why this deployment will not speak, in terms of what to change.

    Named exactly rather than listed: the reasons ask for different things, and
    a message offering all of them points at the wrong one every time but once.
    The address itself is never quoted -- the browser is told what to fix, not
    where this deployment's network keeps its services.
    """
    if config.TTS_MODE != "server":
        return f"this deployment does not speak here (WACTORZ_TTS={config.TTS_MODE})"
    uri = remote.service_uri()
    if remote.is_wyoming_uri(uri) and not remote.WYOMING:
        return "WACTORZ_TTS_URI names a Wyoming synthesiser — pip install 'wactorz[voice]'"
    return "no synthesiser: set WACTORZ_TTS_URI, or pip install 'wactorz[tts]'"


def public_config(_app: web.Application) -> dict[str, Any]:
    """Non-secret TTS config for the browser."""
    # The address is deliberately absent, as it is for recognition: the browser
    # never speaks to the synthesiser, and where it lives is a fact about the
    # network this deployment sits on.
    return {
        "mode": config.TTS_MODE,
        "available": synthesiser_available(),
        # Stripped and `or`-ed rather than given as a default argument: a
        # default applies only when the name is absent, and `.env.template`
        # tells you to leave this one empty for it. `load_dotenv` then supplies
        # "", which reached the synthesiser and was refused, so the documented
        # setup produced no speech at all. Whitespace goes the same way, because
        # a `.env` line keeps trailing spaces more often than anyone means it to.
        "voice": os.getenv("TTS_VOICE", "").strip() or _tts_state.default_voice,
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

    # Mirror TTSManager.ts: strip code blocks, cap at 300 chars.
    text = re.sub(r"```[\s\S]*?```", "code block", text)[:300]

    asked_for = str(body.get("voice") or "").strip()
    configured = os.environ.get("TTS_VOICE", "").strip()

    uri = remote.service_uri()
    try:
        if remote.names_a_service(uri):
            # No fallback to the default below: that name belongs to the
            # synthesiser this process would have used, and a service asked for
            # a voice it has never heard of refuses the whole request. Sending
            # none is how it is asked for the one it is configured with.
            spoken = await remote.synthesise(uri, text, asked_for or configured)
            return web.Response(
                body=spoken.audio,
                content_type=spoken.content_type,
                headers={"Cache-Control": "no-store"},
            )
        voice = asked_for or configured or _tts_state.default_voice
        communicate = edge_tts.Communicate(text, voice)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                data = chunk.get("data")
                if data:
                    chunks.append(data)
        return web.Response(
            body=b"".join(chunks),
            content_type="audio/mpeg",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Logged in full, answered in summary: the exception comes from a
        # third-party service and can carry a URL or a request detail.
        logger.warning("[tts] Synthesis failed: %s", exc)
        return web.Response(status=500, text="Speech synthesis failed")
