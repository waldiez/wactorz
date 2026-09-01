"""REST endpoints for system-level state: health, cost, config, and feeds.

Everything here is about the deployment as a whole rather than an individual
actor. ``config_handler`` is also where each extension's non-secret browser
config is merged in.
"""

import json
import logging
import time
from typing import Any

from aiohttp import web
from aiohttp.web import Response

from ..core import voice_settings
from ..ext import stt, tts
from . import cost, origins, runtime

logger = logging.getLogger(__name__)


async def health_handler(_request: web.Request) -> Response:
    """Liveness probe — 200 as long as the server is accepting requests."""
    return web.json_response({"status": "ok"})


async def cost_handler(_request: web.Request) -> Response:
    """Return spend and message totals for the cost widget."""
    from ..agents.llm_agent import get_global_cost_info

    return web.json_response(get_global_cost_info())


async def cost_limit_handler(request: web.Request) -> Response:
    """Set the spend ceiling that pauses LLM calls once exceeded."""
    from ..agents.llm_agent import set_cost_limit

    try:
        body = await request.json()
        limit_usd = float(body.get("limit_usd", 0))
        period = body.get("period", "monthly")
        if period not in ("daily", "weekly", "monthly"):
            return web.json_response(
                {"error": "period must be daily, weekly, or monthly"}, status=400
            )
        set_cost_limit(limit_usd, period)
        return web.json_response({"ok": True, "limit_usd": limit_usd, "period": period})
    except Exception as exc:
        # Our own words, not the exception's. A caller needs to know what to
        # send differently, and an exception string answers a different
        # question — often with a path or a driver detail attached.
        logger.warning("[api] cost limit rejected: %s", exc)
        return web.json_response(
            {"error": "limit_usd must be a number and period one of daily, weekly, monthly"},
            status=400,
        )


async def cost_reset_handler(_request: web.Request) -> Response:
    """Zero the current spend period without touching the lifetime ledger."""
    from ..agents.llm_agent import reset_global_cost

    try:
        info = reset_global_cost()
        # Clear the in-memory lifetime ledger so max() doesn't pin the display
        # to pre-reset values for the rest of this process lifetime.
        cost.lifetime_cost.clear()
        if runtime.db is not None:
            try:
                runtime.db.kv_delete("_system", cost.LIFETIME_LEDGER_KEY)
            except Exception:
                pass
        return web.json_response({"ok": True, **info})
    except Exception as exc:
        # A failure here is the database's, not the caller's, and a sqlite
        # error carries the file path it was opening.
        logger.exception("[api] cost reset failed: %s", exc)
        return web.json_response({"error": "Could not reset the cost ledger"}, status=500)


async def chat_log_handler(request: web.Request) -> Response:
    """GET /api/chats — query the persistent chat_log table.

    Query params:
      agent   — filter by agent name
      role    — filter by role (user | assistant)
      since   — Unix timestamp float, only return rows newer than this
      limit   — max rows to return (default 200, max 1000)
    """
    if runtime.db is None:
        return web.json_response([], status=200)
    try:
        agent = request.rel_url.query.get("agent")
        role = request.rel_url.query.get("role")
        since = float(request.rel_url.query["since"]) if "since" in request.rel_url.query else None
        limit = min(int(request.rel_url.query.get("limit", 200)), 1000)
        rows = runtime.db.query_chat_log(agent_name=agent, role=role, since=since, limit=limit)
        return web.json_response(rows)
    except Exception as exc:
        logger.exception("[api] chat log query failed: %s", exc)
        return web.json_response({"error": "Could not read the chat log"}, status=500)


async def voice_settings_handler(request: web.Request) -> Response:
    """POST /api/voice — change which branch listens or speaks, while running.

    The environment says what a deployment starts as; this is how someone tries
    another without restarting. Addresses are not settable here on purpose: they
    name services this process dials, and one a browser can write is one it can
    point at anything reachable from this machine.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "expected a JSON body"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "expected a JSON object"}, status=400)

    if body.get("reset"):
        try:
            voice_settings.forget()
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        return web.json_response(_voice_now())

    changed = {k: v for k, v in body.items() if k in {"listening", "speaking", "voice"}}
    if not changed:
        return web.json_response(
            {"error": "nothing to change: listening, speaking or voice"}, status=400
        )
    # Checked before any of it is kept: a request naming one good setting and one
    # bad one would otherwise leave the deployment half-changed, in a state
    # nobody asked for and the answer does not describe.
    for setting, value in changed.items():
        problem = voice_settings.refuses(setting, str(value))
        if problem:
            return web.json_response({"error": problem}, status=400)
    try:
        for setting, value in changed.items():
            voice_settings.choose(setting, str(value))
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)

    # Said again on the way in: a deployment switched to `host` while running has
    # heard nothing about the microphone it now needs.
    _warn_about_the_new_branch()
    return web.json_response(_voice_now())


def _warn_about_the_new_branch() -> None:
    """Repeat the startup checks for whichever branch is now in force."""
    stt.warn_if_it_cannot_listen()
    tts.warn_if_the_room_will_stay_quiet()


def _voice_now() -> dict[str, Any]:
    """What the voice settings resolve to at this moment."""
    return {
        "listening": voice_settings.listening(),
        "speaking": voice_settings.speaking(),
        "voice": voice_settings.voice(),
    }


async def config_handler(request: web.Request) -> Response:
    """Expose non-secret runtime config so the frontend can seed its defaults."""
    from .. import __version__, config
    from ..config import CONFIG
    from ..core import voice_settings
    from ..ext import collect_public_config

    # The /ws proxy is served by *this* server, so point the frontend at the
    # monitor's actual port (WS_PORT), not a hardcoded one.
    raw_host = request.host.split(":")[0]
    ws_host = f"{raw_host}:{runtime.WS_PORT}"
    protocol = "wss" if request.secure else "ws"

    ws_url = f"{protocol}://{ws_host}/ws"

    payload: dict = {
        # Which Wactorz this is. The dashboard is served from the same wheel, but
        # a browser can hold an old page for a long time, so the version it shows
        # is the running one rather than the one it was built from.
        "version": __version__,
        "ha": {
            # URL only — the dashboard links out to the HA UI and never talks to
            # HA directly, so the long-lived token must NOT reach the browser.
            "url": CONFIG.ha_url,
        },
        "llm": {
            "provider": CONFIG.llm_provider,
            "model": CONFIG.llm_model,
        },
        "weather": {
            "defaultLocation": CONFIG.weather_default_location,
        },
        # The upload routes are only registered when this is on, so the browser
        # has to learn it from the server rather than from how it was built —
        # otherwise a deployment with uploads off still offers a drop zone that
        # can only fail, and one with uploads on hides a feature it has.
        "uploads": {"enabled": config.UPLOADS_ENABLED},
        # Which speech-to-text branch is offered, for the same reason as uploads:
        # a bundle cannot know, and the microphone must appear only where it can
        # actually work. "off" means the browser shows no microphone at all.
        "stt": {"mode": voice_settings.listening()},
        # Whether this browser holds a session it could end, which is not the
        # same question as "is a key configured". Under Home Assistant ingress
        # the user was authenticated by HA and carries no session here, so a
        # sign-out would end nothing while implying it had — and on an install
        # with no key there is nothing to sign out of at all.
        "auth": {"canSignOut": bool(CONFIG.api_key) and not origins.from_supervisor(request)},
        "ws_url": ws_url,
    }
    # Merge each extension's non-secret browser config (e.g. tts availability),
    # namespaced by extension name. Merged into an existing key rather than
    # replacing it: an extension named after something core already reports would
    # otherwise delete core's fields on its way in, silently and only in the
    # deployments that have it installed. Speech-to-text is exactly that case --
    # which branch is offered is core's to answer, whether a recogniser is
    # reachable is the extension's, and they are two halves of one question.
    for key, value in collect_public_config(request.app).items():
        existing = payload.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            existing.update(value)
        else:
            payload[key] = value
    return web.json_response(payload)


async def feed_handler(_request: web.Request) -> Response:
    """Return recent chat events for the UI feed, with REAL persisted timestamps.

    Previously this read from kv_store.conversation_history, which is just a
    JSON list with no timestamps — so each entry got `i` (the loop index) as
    its timestamp and the frontend re-dated them to "now - i*delta", causing
    timestamps to reset on every page reload / restart.

    Now we read from the chat_log table, which has a real `ts REAL` column
    written at the moment each turn happens. Falls back to the legacy
    kv_store path only if chat_log is empty (e.g. a freshly upgraded DB
    where nothing has been written yet) so existing users still see their
    pre-upgrade history on first launch.
    """
    if runtime.db is None:
        return web.json_response([])
    try:
        # Primary path — persistent chat_log with real timestamps.
        try:
            rows = runtime.db.query_chat_log(limit=50)
        except Exception as exc:
            logger.warning("[feed] chat_log query failed: %s", exc)
            rows = []

        if rows:
            # query_chat_log returns newest-first; the frontend expects
            # chronological (oldest-first) so the latest message ends up
            # at the bottom of the feed.
            rows = list(reversed(rows))
            items = [
                {
                    "type": "chat",
                    "label": str(r.get("content", "")),
                    "agentName": r.get("agent_name", ""),
                    "role": r.get("role", ""),
                    "timestamp": float(r.get("ts", 0.0)),  # REAL Unix time, not an index
                    "_seq": i,
                    "_agent": r.get("agent_name", ""),
                }
                for i, r in enumerate(rows)
            ]
            return web.json_response(items)

        # Fallback — legacy kv_store path. Keeps old DBs displaying *something*
        # until new chat turns start populating chat_log. Synthesises a
        # timestamp by anchoring the last entry to "now" and walking backwards
        # in 1-second steps, so at least entries are ordered consistently.
        kv_rows = runtime.db.conn.execute(
            "SELECT agent, value FROM kv_store WHERE key='conversation_history'"
        ).fetchall()
        items = []
        now = time.time()
        for agent_name, value in kv_rows:
            try:
                history = json.loads(value)
                visible = [
                    m
                    for m in history
                    if isinstance(m, dict) and m.get("role") in ("user", "assistant")
                ]
                n = len(visible)
                for i, msg in enumerate(visible):
                    items.append(
                        {
                            "type": "chat",
                            "label": str(msg.get("content", "")),
                            "agentName": agent_name,
                            "role": msg.get("role", ""),
                            # Synthesised but at least monotonic and anchored
                            # to a real wall-clock value, not a bare index.
                            "timestamp": now - (n - 1 - i),
                            "_seq": i,
                            "_agent": agent_name,
                        }
                    )
            except Exception:
                pass
        return web.json_response(items[-50:])
    except Exception as exc:
        logger.warning("[feed] handler failed: %s", exc)
        return web.json_response([])
