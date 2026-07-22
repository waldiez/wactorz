"""REST endpoints for system-level state: health, cost, config, and feeds.

Everything here is about the deployment as a whole rather than an individual
actor. ``config_handler`` is also where each extension's non-secret browser
config is merged in.
"""

import json
import logging
import time

from aiohttp import web
from aiohttp.web import Response

from . import cost, runtime

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
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


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
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


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
        return web.json_response({"error": str(exc)}, status=500)


async def config_handler(request: web.Request) -> Response:
    """Expose non-secret runtime config so the frontend can seed its defaults."""
    from ..config import CONFIG
    from ..ext import collect_public_config

    # The /ws proxy is served by *this* server, so point the frontend at the
    # monitor's actual port (WS_PORT), not a hardcoded one.
    raw_host = request.host.split(":")[0]
    ws_host = f"{raw_host}:{runtime.WS_PORT}"
    protocol = "wss" if request.secure else "ws"

    ws_url = f"{protocol}://{ws_host}/ws"

    payload: dict = {
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
        "ws_url": ws_url,
    }
    # Merge each extension's non-secret browser config (e.g. tts availability),
    # namespaced by extension name.
    payload.update(collect_public_config(request.app))
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
