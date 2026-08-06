"""TTS extension — server-side text-to-speech via edge-tts.

Optional dependency: ``pip install wactorz[tts]`` (installs edge-tts).
If edge-tts is not installed the extension still loads — routes return 503
and ``public_config()`` reports ``available: false`` so the frontend falls
back to browser Web Speech API.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from aiohttp import web

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


def public_config(_app: web.Application) -> dict[str, Any]:
    """Non-secret TTS config for the browser."""
    return {
        "available": _tts_state.available,
        "voice": os.getenv("TTS_VOICE", _tts_state.default_voice),
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
    """GET /api/tts/voices — list available edge-tts voices."""
    if _tts_state.voices is None:
        await _warm_tts_voices()
    return web.json_response(_tts_state.voices or [])


async def tts_handler(request: web.Request) -> web.Response:
    """POST /api/tts {"text": ..., "voice": ...} — synthesize speech via edge-tts.

    Returns audio/mpeg. 503 if edge-tts is not installed so the frontend
    falls back to the Web Speech API transparently.

    POST rather than GET because this route does work: each call synthesizes
    audio through an outbound service. A GET is assumed to be a read, and any
    page can fire one cross-origin with no `Origin` header at all — `<img
    src="…/api/tts?text=…">` is enough — which is exactly the assumption an
    Origin check relies on. A POST carries a body, so it cannot be triggered
    that way.
    """
    if not _tts_state.available:
        return web.Response(
            status=503,
            text="edge-tts not installed — pip install 'wactorz[tts]'",
        )

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

    default_voice = os.environ.get("TTS_VOICE", _tts_state.default_voice)
    voice = str(body.get("voice") or "") or default_voice

    try:
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
        logger.warning("[tts] Synthesis failed: %s", exc)
        return web.Response(status=500, text=str(exc))
