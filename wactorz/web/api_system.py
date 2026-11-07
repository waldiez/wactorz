# SPDX-License-Identifier: Apache-2.0
"""REST endpoints for system-level state: health, cost, config, and feeds.

Everything here is about the deployment as a whole rather than an individual
actor. ``config_handler`` is also where each extension's non-secret browser
config is merged in.
"""

import asyncio
import logging
import math
import time
from typing import Any

from aiohttp import web
from aiohttp.web import Response

from .. import installed
from ..agents.llm.cost import get_global_cost_info, reset_global_cost, set_cost_limit
from . import cost, origins, panels, probes, questions, runtime
from .metrics import known_nodes

logger: logging.Logger = logging.getLogger(__name__)

#: Rows ``GET /api/chats`` sends when not told, and the most it sends.
DEFAULT_CHAT_ROWS = 200
MAX_CHAT_ROWS = 1000


async def readiness_handler(_request: web.Request) -> Response:
    """Readiness probe — 200 once this process should be sent traffic, 503 until then.

    A monitor with no actor system in its process reports on the broker link it
    listens on, which is all it depends on.
    """
    if runtime.system is None:
        checks = {"broker": probes.broker_check(runtime.mqtt_connected)}
    else:
        checks = await probes.readiness(runtime.system)
    return probes.readiness_response(checks)


async def nodes_handler(_request: web.Request) -> Response:
    """``GET /api/nodes``: every node main knows, its readings and what its machine is.

    The dashboard hears node heartbeats as they arrive, but a node's manifest
    is retained and reaches this server once, when it subscribes; this is where
    a page that opens later reads it. Empty without main, which is what knows
    the nodes.
    """
    return web.json_response({"nodes": known_nodes()})


async def cost_handler(_request: web.Request) -> Response:
    """Return spend and message totals for the cost widget."""
    return web.json_response(await asyncio.to_thread(get_global_cost_info))


async def cost_limit_handler(request: web.Request) -> Response:
    """Set the spend ceiling that pauses LLM calls once exceeded."""
    try:
        body = await request.json()
        limit_usd = float(body.get("limit_usd", 0))
        period = body.get("period", "monthly")
        if period not in ("daily", "weekly", "monthly"):
            return web.json_response(
                {"error": "period must be daily, weekly, or monthly"}, status=400
            )
        await asyncio.to_thread(set_cost_limit, limit_usd, period)
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
    try:
        info = await asyncio.to_thread(reset_global_cost)
        # Cleared in memory now, and in SQLite after any save already queued, so
        # neither a save before the reset nor one after it is lost or undone.
        cost.reset_lifetime()
        await cost.settled_totals()
        return web.json_response({"ok": True, **info})
    except Exception:
        # A failure here is the database's, not the caller's, and a sqlite
        # error carries the file path it was opening.
        logger.exception("[api] cost reset failed")
        return web.json_response({"error": "Could not reset the cost ledger"}, status=500)


def with_question(row: dict[str, Any]) -> dict[str, Any]:
    """``row``, with the question it is when the ledger still knows it.

    A chat-log row keeps only the words an agent sent, so a question comes
    back without its buttons; the ledger has the rest. The row then says which
    question, from which agent, offering what, and the pick once there is one.
    """
    if row.get("role") != "assistant":
        return row
    asked = questions.asked_in(
        str(row.get("agent_name", "")), str(row.get("content", "")), float(row.get("ts") or 0.0)
    )
    if asked is None:
        return row
    agent_id, question = asked
    return {
        **row,
        "question": {
            "id": question.id,
            "agent_id": agent_id,
            "text": question.text,
            "options": list(question.options),
            "answer": question.answer,
        },
    }


async def chat_log_handler(request: web.Request) -> Response:
    """GET /api/chats — query the persistent chat_log table.

    Query params:
      agent_id — one agent's history, by its id
      agent   — filter by agent name: every agent that went by it
      role    — filter by role (user | assistant)
      since   — Unix timestamp float, only return rows newer than this
      limit   — max rows to return (default 200, max 1000)

    A row that is a question the ledger knows carries it as ``question``.
    """
    if runtime.db is None:
        return web.json_response([], status=200)
    try:
        agent = request.rel_url.query.get("agent")
        agent_id = request.rel_url.query.get("agent_id")
        role = request.rel_url.query.get("role")
        since = _since(request.rel_url.query.get("since"))
        limit = _chat_limit(request.rel_url.query.get("limit"))
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    try:
        rows = await asyncio.to_thread(
            runtime.db.query_chat_log,
            agent_name=agent,
            agent_id=agent_id,
            role=role,
            since=since,
            limit=limit,
        )
        return web.json_response([with_question(row) for row in rows])
    except Exception:
        logger.exception("[api] chat log query failed")
        return web.json_response({"error": "Could not read the chat log"}, status=500)


class _BadChatQueryError(ValueError):
    """A query value ``GET /api/chats`` cannot use, and why."""

    @classmethod
    def not_a_limit(cls) -> "_BadChatQueryError":
        return cls(f"'limit' is a whole number from 1 (at most {MAX_CHAT_ROWS} are sent)")

    @classmethod
    def not_a_time(cls) -> "_BadChatQueryError":
        return cls("'since' is Unix seconds")


def _chat_limit(raw: str | None) -> int:
    """How many rows to send: at most ``MAX_CHAT_ROWS``. SQLite reads a negative limit as none."""
    if raw is None or raw == "":
        return DEFAULT_CHAT_ROWS
    try:
        value = int(raw)
    except ValueError:
        raise _BadChatQueryError.not_a_limit() from None
    if value < 1:
        raise _BadChatQueryError.not_a_limit()
    return min(value, MAX_CHAT_ROWS)


def _since(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        raise _BadChatQueryError.not_a_time() from None
    if not math.isfinite(value):
        raise _BadChatQueryError.not_a_time()
    return value


async def config_handler(request: web.Request) -> Response:
    """Expose non-secret runtime config so the frontend can seed its defaults."""
    from .. import __version__, config
    from ..config import CONFIG
    from ..ext import collect_public_config

    # The /ws proxy is served by *this* server, so point the frontend at the
    # monitor's actual port (WS_PORT), not a hardcoded one.
    raw_host = request.host.split(":")[0]
    ws_host = f"{raw_host}:{runtime.WS_PORT}"
    protocol = "wss" if request.secure else "ws"

    ws_url = f"{protocol}://{ws_host}/ws"

    payload: dict[str, Any] = {
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
        # Whether this browser holds a session it could end, which is not the
        # same question as "is a key configured". Under Home Assistant ingress
        # the user was authenticated by HA and carries no session here, so a
        # sign-out would end nothing while implying it had — and on an install
        # with no key there is nothing to sign out of at all.
        "auth": {"canSignOut": bool(CONFIG.api_key) and not origins.from_supervisor(request)},
        "ws_url": ws_url,
        # Names of packages left out at start, so the page can say how to load one.
        "extensions": {"blocked": installed.blocked(), "panels": panels.listed(request.app)},
    }
    # Merge each extension's non-secret browser config (e.g. tts availability),
    # namespaced by extension name.
    payload.update(collect_public_config(request.app))
    return web.json_response(payload)


async def feed_handler(_request: web.Request) -> Response:
    """Recent chat turns for the activity feed, oldest first, each at the time it happened.

    Read from the chat log, whose rows carry a real time; a database with no
    chat log rows yet falls back to the conversations agents kept, with times
    made up in order (`_feed_items`). Read on a thread: the database is a file.
    """
    if runtime.db is None:
        return web.json_response([])
    try:
        return web.json_response(await asyncio.to_thread(_feed_items))
    except Exception as exc:
        logger.warning("[feed] handler failed: %s", exc)
        return web.json_response([])


def _feed_items() -> list[dict[str, Any]]:
    """The feed's rows from the database. Run on a thread."""
    db = runtime.db
    if db is None:
        return []
    try:
        rows = db.query_chat_log(limit=50)
    except Exception as exc:
        logger.warning("[feed] chat_log query failed: %s", exc)
        rows = []
    if rows:
        # Newest first from the query; the feed shows the latest at the bottom.
        return [
            {
                "type": "chat",
                "label": str(r.get("content", "")),
                "agentName": r.get("agent_name", ""),
                "role": r.get("role", ""),
                "timestamp": float(r.get("ts", 0.0)),
                "_seq": i,
                "_agent": r.get("agent_name", ""),
            }
            for i, r in enumerate(reversed(rows))
        ]
    # What agents kept has no times: anchored to now and walked back a second a
    # turn, so the order at least holds.
    items: list[dict[str, Any]] = []
    now = time.time()
    for agent_name, history in db.kv_values("conversation_history"):
        if not isinstance(history, list):
            continue
        visible = [
            m for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant")
        ]
        n = len(visible)
        items.extend(
            {
                "type": "chat",
                "label": str(msg.get("content", "")),
                "agentName": agent_name,
                "role": msg.get("role", ""),
                "timestamp": now - (n - 1 - i),
                "_seq": i,
                "_agent": agent_name,
            }
            for i, msg in enumerate(visible)
        )
    return items[-50:]
