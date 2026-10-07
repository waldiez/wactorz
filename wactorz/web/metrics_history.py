"""Sampling every agent and node into the metrics history, about once a minute.

The samples are what the dashboard knows already -- each agent's latest
heartbeat and metrics frame, each node's latest heartbeat -- written down, so a
trend survives a restart, and an install without Prometheus (the Home Assistant
add-on) has one at all. Nothing new travels the broker for it.

A minute rather than every heartbeat: a trend reads the same at that resolution,
and an agent heartbeats several times a minute, which on a Raspberry Pi's card
is several times the writes for nothing a chart would show.

An agent is sampled while the dashboard has heard from it lately. One that has
gone quiet -- stopped, deleted, or on a node that went away -- stops being
sampled rather than being written down unchanged for as long as it is listed.
"""

import asyncio
import logging
import math
import time
from typing import Any

from aiohttp import web

from .. import config
from . import runtime
from .metrics import known_nodes

logger = logging.getLogger(__name__)

#: How often the history is sampled.
SAMPLE_EVERY_S = 60.0

#: How far back a history request looks when it does not say.
DEFAULT_HOURS = 24.0

#: The most samples one request returns: a week of one agent at one a minute.
MAX_SAMPLES = 10_080

#: How recently an agent must have been heard from to be sampled: two heartbeat
#: intervals and some, so one heartbeat late is not a gap in its trend.
HEARD_WITHIN_S = 120.0


def agent_samples(agents: dict[str, dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """One sample per agent the dashboard has heard from lately."""
    samples = []
    for entry in agents.values():
        if now - float(entry.get("last_update") or 0) > HEARD_WITHIN_S:
            continue
        frame = entry.get("metrics")
        if not isinstance(frame, dict):
            frame = {}
        samples.append(
            {
                "ts": now,
                "agent": str(entry.get("name") or entry.get("agent_id") or ""),
                "node": str(entry.get("node") or ""),
                "state": str(entry.get("state") or ""),
                "memory_mb": _number(entry.get("mem")),
                "messages_processed": _number(entry.get("messages_processed")),
                "errors": _number(frame.get("errors")),
                "tasks_completed": _number(frame.get("tasks_completed")),
                "tasks_failed": _number(frame.get("tasks_failed")),
                "cost_usd": _number(entry.get("cost_usd", frame.get("cost_usd"))),
                "queue_wait_p95_s": _number(frame.get("queue_wait_p95_s")),
                "message_p95_s": _number(frame.get("message_p95_s")),
                "task_p95_s": _number(frame.get("task_p95_s")),
            }
        )
    return samples


def node_samples(nodes: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """One sample per node main knows, online or not."""
    return [
        {
            "ts": now,
            "node": str(node.get("node") or node.get("name") or ""),
            "online": 1 if node.get("online") else 0,
            "cpu_pct": _number(node.get("cpu_pct")),
            "mem_used_mb": _number(node.get("mem_used_mb")),
            "mem_free_mb": _number(node.get("mem_free_mb")),
            "agents": len(node.get("agents") or []),
        }
        for node in nodes
    ]


async def record_once() -> int:
    """Write one sample of everything, off the event loop. Returns the rows written."""
    db = runtime.db
    if db is None:
        return 0
    now = time.time()
    agents = agent_samples(runtime.state["agents"], now)
    nodes = node_samples(known_nodes(), now)
    if not agents and not nodes:
        return 0
    await asyncio.to_thread(db.write_metrics_history, agents, nodes)
    return len(agents) + len(nodes)


async def record_loop() -> None:
    """Sample for as long as the dashboard's server runs.

    A sample that fails is said and the next one tried: a full disk or a locked
    database is a reason to miss a minute of history, not to stop keeping it.
    """
    while True:
        await asyncio.sleep(SAMPLE_EVERY_S)
        try:
            await record_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[history] Could not record the metrics sample", exc_info=True)


async def agent_history_handler(request: web.Request) -> web.Response:
    """``GET /api/history/agents/{name}?hours=24``: one agent's samples, oldest first."""
    return await _history(request, "agent")


async def node_history_handler(request: web.Request) -> web.Response:
    """``GET /api/history/nodes/{name}?hours=24``: one node's samples, oldest first."""
    return await _history(request, "node")


async def _history(request: web.Request, kind: str) -> web.Response:
    db = runtime.db
    if db is None:
        return web.json_response({"error": "no database: the history is not kept"}, status=503)
    name = request.match_info["name"]
    try:
        hours = float(request.query.get("hours", DEFAULT_HOURS))
    except ValueError:
        return web.json_response({"error": "hours must be a number"}, status=400)
    if not (hours > 0 and math.isfinite(hours)):
        return web.json_response({"error": "hours must be above 0 and finite"}, status=400)
    since = time.time() - hours * 3600
    query = db.query_agent_history if kind == "agent" else db.query_node_history
    samples = await asyncio.to_thread(query, name, since, MAX_SAMPLES)
    return web.json_response(
        {
            kind: name,
            "hours": hours,
            "kept_days": config.RETENTION_METRICS_DAYS,
            "sample_every_s": SAMPLE_EVERY_S,
            "samples": samples,
        }
    )


def _number(value: Any) -> float | None:
    """``value`` as a number for the history, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
