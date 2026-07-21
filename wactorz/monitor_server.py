"""Wactorz Monitor — WebSocket dashboard + optional MQTT bridge.

Chat routing modes (set via registry wiring in cli.py):
  direct_ws  — registry is set; chat goes straight to actors over WebSocket.
               No IOAgent, no MQTT round-trip for user messages.
  mqtt       — registry is None; chat goes through IOAgent via MQTT (legacy).

The mode is advertised to the browser on connect via a {"type":"config"} frame
so the frontend knows whether to send chat over /ws or publish to io/chat.
"""

# pylint: disable=global-statement,invalid-name,logging-fstring-interpolation
# pylint: disable=broad-exception-caught,protected-access,line-too-long
# pylint: disable=missing-function-docstring,unused-argument,too-many-lines
# pylint: disable=import-outside-toplevel,wrong-import-position,wrong-import-order

# pyright: reportAttributeAccessIssue=false,reportUnusedParameter=false,reportUnusedImport=false

import asyncio
import json
import logging
import os
import sys
import time

from aiohttp import web
from aiohttp.typedefs import Handler

from ._bootstrap import WACTORZ_BOOTSTRAP  # noqa: F401 # pylint: disable=unused-import
from .monitor import chat, cost, events, lifecycle, mqtt, runtime, static_site, ws

Response = web.Response | web.FileResponse | web.StreamResponse


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── helpers ────────────────────────────────────────────────────────────────
async def _check_ws_port() -> bool:
    """Return True if WS_PORT is free to bind."""
    try:
        server = await asyncio.start_server(lambda r, w: None, "0.0.0.0", runtime.WS_PORT)
        server.close()
        await server.wait_closed()
        return True
    except OSError as exc:
        logger.error(f"[startup] Port {runtime.WS_PORT} already in use — {exc}")
        return False


def _actor_payload(ag: dict) -> dict:
    return {
        "id": ag.get("agent_id", ""),
        "name": ag.get("name", ""),
        "state": ag.get("state", "unknown"),
        "protected": ag.get("protected", False),
        "cpu": ag.get("cpu"),
        "mem": ag.get("mem"),
        "task": ag.get("task"),
        "messagesProcessed": ag.get("messages_processed"),
        "costUsd": ag.get("cost_usd"),
    }


async def health_handler(request: web.Request) -> Response:
    return web.json_response({"status": "ok"})


async def cost_handler(request: web.Request) -> Response:
    from .agents.llm_agent import get_global_cost_info

    return web.json_response(get_global_cost_info())


async def cost_limit_handler(request: web.Request) -> Response:
    from .agents.llm_agent import set_cost_limit

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


async def cost_reset_handler(request: web.Request) -> Response:
    from .agents.llm_agent import reset_global_cost

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


async def send_message_handler(request: web.Request) -> Response:
    actor_id = request.match_info["actor_id"]
    if runtime.registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    content = data.get("content", "").strip()
    if not content:
        return web.json_response({"error": "content required"}, status=400)
    actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
    if actor is None:
        return web.json_response({"error": "actor not found"}, status=404)
    # This endpoint names an explicit target, but chat.route_chat re-derives the
    # target from the text and defaults to main — so without this the addressed
    # actor is dropped. Prepend the mention to route there, unless the caller
    # already addressed someone (@) or it's a slash command (/).
    routed = content if content.startswith(("@", "/")) else f"@{actor.name} {content}"
    chat.track_chat_task(asyncio.create_task(chat.route_chat(routed, lambda t: None)))
    return web.json_response({"status": "sent"})


async def delete_actor_handler(request: web.Request) -> Response:
    actor_id = request.match_info["actor_id"]
    # Resolve the dashboard's record first so remote agents (which aren't in
    # the local registry) can still be deleted via this endpoint. The earlier
    # 503/404 short-circuit made remote deletes impossible.
    record = runtime.state["agents"].get(actor_id) or {}
    if not record:
        # Fall back to local-registry lookup so a name-based ID still works.
        if runtime.registry is not None:
            actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
            if actor is None:
                return web.json_response({"error": "actor not found"}, status=404)
            if getattr(actor, "protected", False):
                return web.json_response({"error": "actor is protected"}, status=403)
            actor_id = actor.actor_id
        else:
            return web.json_response({"error": "actor not found"}, status=404)
    if record.get("protected"):
        return web.json_response({"error": "actor is protected"}, status=403)
    routed = await lifecycle.delete_agent(actor_id)
    await ws.broadcast(
        {"type": "lifecycle.delete_agent", "agent_id": actor_id, "state": events.snapshot()}
    )
    return web.Response(status=200, text=f"stopping ({routed})")


# Home-Assistant system agents: supervised on a fresh boot like the protected
# actors, but intentionally left NON-protected so a user can still delete one
# individually. A factory reset must keep them anyway (a clean boot recreates
# them), so they are named explicitly here rather than inferred from protection.
_HA_SYSTEM_AGENTS = frozenset(
    {
        "home-assistant-agent",
        "home-assistant-map-agent",
        "home-assistant-state-bridge",
    }
)


def survives_factory_reset(name: str, protected: bool) -> bool:
    """Whether an agent is kept by ``reset all`` (factory reset).

    Kept if it is a system-protected actor (main / monitor / io-agent / installer
    / catalog) OR one of the HA system agents — i.e. exactly the set a fresh,
    empty boot brings up. Everything else (user- and catalog-spawned agents) is
    wiped. Note this is independent of manual delete, which keys off ``protected``
    alone, so the HA agents stay individually deletable.
    """
    return protected or name in _HA_SYSTEM_AGENTS


async def reset_handler(request: web.Request) -> Response:
    """POST /api/reset  —  clear stored state and ws.broadcast a reset event.

    Body (JSON):
      scope   : "chat" | "state" | "metrics" | "spawns" | "all"  (required)
      agent   : str  (optional — limit to one agent by name)
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    scope = body.get("scope", "")
    agent = body.get("agent") or None

    valid = {"chat", "state", "metrics", "spawns", "logs", "all"}
    if scope not in valid:
        return web.json_response({"error": f"scope must be one of {sorted(valid)}"}, status=400)

    import wactorz.reset as _reset

    if scope == "all":
        # Block incoming heartbeats, stop all actors, wipe disk, clear memory
        runtime.hard_resetting = True
        # Wrap the whole teardown so _hard_resetting ALWAYS resets — otherwise a
        # mid-wipe exception leaves it True forever and every incoming heartbeat
        # stays blocked, freezing the dashboard until the process restarts.
        try:
            supervisor = (
                getattr(runtime.registry, "_supervisor_ref", None)
                if runtime.registry is not None
                else None
            )
            all_actors = list(runtime.registry.all_actors()) if runtime.registry is not None else []

            # Factory reset keeps the protected system actors and the HA system
            # agents; only user- and catalog-spawned agents are stopped.
            def _kept(name: str, protected: bool) -> bool:
                return survives_factory_reset(name, protected)

            stoppable = [a for a in all_actors if not _kept(a.name, getattr(a, "protected", False))]
            # The registry is the AUTHORITATIVE source of the protected flag — the
            # dashboard entry's "protected" is only set when a heartbeat happened to
            # carry it, so trusting it alone wrongly tears down system agents (main /
            # monitor / installer / catalog / the HA family) and they flicker back.
            kept_ids = {
                a.actor_id for a in all_actors if _kept(a.name, getattr(a, "protected", False))
            }
            kept_names = {
                a.name for a in all_actors if _kept(a.name, getattr(a, "protected", False))
            }
            # Tear down every non-kept agent the dashboard knows about (covers an
            # agent present only via MQTT, or a remote agent absent from this registry),
            # but never a fresh-boot or protected one.
            dash_ids = [
                aid
                for aid, ag in runtime.state["agents"].items()
                if not _kept(ag.get("name", ""), ag.get("protected", False))
                and aid not in kept_ids
                and ag.get("name") not in kept_names
            ]
            agent_ids = list({a.actor_id for a in stoppable} | set(dash_ids))

            # Release supervised actors first so the Supervisor doesn't race to
            # restart them, then stop + unregister the live local ones.
            if supervisor is not None:
                for actor in stoppable:
                    supervisor.release(actor.name)
            await asyncio.gather(*[actor.stop() for actor in stoppable], return_exceptions=True)
            await asyncio.gather(
                *[runtime.registry.unregister(a.actor_id) for a in stoppable if runtime.registry],
                return_exceptions=True,
            )

            # Stop agents living on runner nodes (not in this registry) and clear the
            # retained spawn directives that would otherwise replay on reconnect.
            # Harmless when there are no nodes.
            node_names = set(runtime.state["nodes"].keys())
            main_actor = chat.find_main()
            if main_actor is not None and hasattr(main_actor, "_get_spawn_registry"):
                for cfg in (main_actor._get_spawn_registry() or {}).values():
                    n = (cfg.get("node") or "").strip()
                    if n:
                        node_names.add(n)
            if runtime.mqtt_client_ref and node_names:
                await asyncio.gather(
                    *[
                        runtime.mqtt_client_ref.publish(
                            f"nodes/{n}/stop_all", json.dumps({"reason": "wipe everything"}), qos=1
                        )
                        for n in node_names
                    ],
                    return_exceptions=True,
                )
                await asyncio.gather(
                    *[
                        runtime.mqtt_client_ref.publish(f"nodes/{n}/spawn", b"", retain=True)
                        for n in node_names
                    ],
                    return_exceptions=True,
                )

            # Purge retained MQTT for EVERY non-protected agent, tombstone each so a
            # late/in-flight frame can't re-admit it once _hard_resetting clears, and
            # drop it from the dashboard now.
            await asyncio.gather(
                *[lifecycle.purge_agent_retained(aid) for aid in agent_ids],
                return_exceptions=True,
            )
            for aid in agent_ids:
                runtime.mark_deleted(aid)
                runtime.state["agents"].pop(aid, None)

            # Clear the live spawn registry + retained desired_state so neither a
            # restart nor a runner reconnect can resurrect the wiped agents. Runs
            # before reset_all() wipes the kv on disk (it reads the registry first).
            await lifecycle.purge_spawn_reconcile(None)
            # Reset in-memory metrics + history on the KEPT actors (protected +
            # fresh-boot) so they too return to factory state, not just stay alive.
            kept_actors = [a for a in all_actors if _kept(a.name, getattr(a, "protected", False))]
            for actor in kept_actors:
                actor.metrics.messages_processed = 0
                actor.metrics.errors = 0
                actor.metrics.tasks_completed = 0
                actor.metrics.tasks_failed = 0
                cost.reset_actor_cost(actor)
                if hasattr(actor, "_conversation_history"):
                    actor._conversation_history = []
                if hasattr(actor, "_history_summary"):
                    actor._history_summary = ""
                if hasattr(actor, "_user_facts"):
                    actor._user_facts = {}
                if hasattr(actor, "_pipeline_rules"):
                    actor._pipeline_rules = []
                # NOTE: the kv-backed spawn registry is cleared by
                # _purge_spawn_reconcile() above — assigning the _spawned_agents
                # attribute was a no-op (the registry is read via recall()).
            _reset.reset_all(agent)
            # Clear the in-memory lifetime cost ledger + global accumulator too, or
            # the headline total re-pins to the old high-water once _hard_resetting
            # clears and heartbeats resume (mirrors /api/cost/reset).
            cost.lifetime_cost.clear()
            try:
                from .agents.llm_agent import reset_global_cost

                reset_global_cost()
            except Exception as exc:
                logger.debug("[reset] reset_global_cost skipped: %s", exc)
            runtime.state["agents"].clear()
            runtime.state["nodes"].clear()
            runtime.state["alerts"].clear()
            runtime.state["log_feed"].clear()
            await ws.broadcast(
                {
                    "type": "reset",
                    "scope": "all",
                    "agent": None,
                    "state": {
                        "agents": [],
                        "nodes": [],
                        "alerts": [],
                        "log_feed": [],
                        "total_cost_usd": 0,
                        "total_messages": 0,
                    },
                }
            )
        finally:
            runtime.hard_resetting = False
        return web.json_response({"status": "ok", "scope": "all", "agent": None})
    if scope == "chat":
        _reset.reset_chat(agent)
        # Also clear the LIVE in-memory conversation on running actors. reset_chat
        # only clears the persisted chat_log/kv, so without this the agent still
        # "remembers" the conversation (and re-persists it on the next turn) until
        # a restart — the same live-vs-disk gap the metrics scope guards against.
        live_actors = list(runtime.registry.all_actors()) if runtime.registry is not None else []
        for actor in live_actors:
            if agent and actor.name != agent:
                continue
            if hasattr(actor, "_conversation_history"):
                actor._conversation_history = []
            if hasattr(actor, "_history_summary"):
                actor._history_summary = ""
    elif scope == "state":
        if agent:
            _reset.reset_agent_state(agent)
        else:
            _reset._reset_all_pickles()
    elif scope == "metrics":
        _reset.reset_metrics(agent)
        # Also zero the LIVE in-memory counters on running actors. reset_metrics
        # only clears the persisted kv snapshot, so without this the next
        # heartbeat re-reports the old totals and the dashboard looks unchanged
        # until a restart. Scoped to metrics/cost fields only (not history).
        live_actors = list(runtime.registry.all_actors()) if runtime.registry is not None else []
        for actor in live_actors:
            if agent and actor.name != agent:
                continue
            actor.metrics.messages_processed = 0
            cost.reset_actor_cost(actor)
        # The headline total is max(live + historical, lifetime ledger).
        # reset_metrics cleared the kv ledger, but the in-memory cost.lifetime_cost
        # high-water survives in THIS process and pins the headline to its old
        # value — so the total only "drops" by the live component and never
        # zeroes. Clear it here, mirroring /api/cost/reset.
        if agent:
            aid = next((getattr(a, "actor_id", None) for a in live_actors if a.name == agent), None)
            if aid:
                cost.lifetime_cost.pop(aid, None)
        else:
            cost.lifetime_cost.clear()
            try:
                from .agents.llm_agent import reset_global_cost

                reset_global_cost()
            except Exception as exc:
                logger.debug("[reset] reset_global_cost skipped: %s", exc)
    elif scope == "spawns":
        # Clear live state + retained desired_state FIRST (it needs the registry
        # to learn the affected nodes), then wipe the kv on disk. Without this,
        # clearing the registry left desired_state behind and a runner reconnect
        # reconciled the agents straight back.
        await lifecycle.purge_spawn_reconcile(agent)
        _reset.reset_spawns(agent)
    elif scope == "logs":
        _reset.reset_logs()
        # The activity feed mirrors the log files we just truncated, so drop the
        # in-memory entries too — otherwise the UI keeps showing stale lines.
        runtime.state["log_feed"].clear()

    # Clear in-memory dashboard cost/message state for the affected agents.
    # Scoped to "metrics" only — clearing chat history must not zero cost/
    # message counters or wipe the alerts/activity feed.
    if scope == "metrics":
        if agent:
            aid = next(
                (k for k, v in runtime.state["agents"].items() if v.get("name") == agent), None
            )
            if aid:
                runtime.state["agents"][aid].pop("cost_usd", None)
                runtime.state["agents"][aid].pop("messages_processed", None)
        else:
            for aid in runtime.state["agents"]:
                runtime.state["agents"][aid].pop("cost_usd", None)
                runtime.state["agents"][aid].pop("messages_processed", None)
            runtime.state["alerts"].clear()
            runtime.state["log_feed"].clear()

    await ws.broadcast(
        {"type": "reset", "scope": scope, "agent": agent, "state": events.snapshot()}
    )
    return web.json_response({"status": "ok", "scope": scope, "agent": agent})


async def pause_actor_handler(request: web.Request) -> Response:
    actor_id = request.match_info["actor_id"]
    if runtime.registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
    if actor is None:
        return web.json_response({"error": "actor not found"}, status=404)
    if getattr(actor, "protected", False):
        return web.json_response({"error": "actor is protected"}, status=403)
    if runtime.mqtt_client_ref:
        await runtime.mqtt_client_ref.publish(
            f"agents/{actor_id}/commands",
            json.dumps({"command": "pause", "sender": "api", "timestamp": time.time()}),
        )
    return web.json_response({"status": "pausing"})


async def resume_actor_handler(request: web.Request) -> Response:
    actor_id = request.match_info["actor_id"]
    if runtime.registry is None:
        return web.json_response({"error": "registry not available"}, status=503)
    actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
    if actor is None:
        return web.json_response({"error": "actor not found"}, status=404)
    if getattr(actor, "protected", False):
        return web.json_response({"error": "actor is protected"}, status=403)
    if runtime.mqtt_client_ref:
        await runtime.mqtt_client_ref.publish(
            f"agents/{actor_id}/commands",
            json.dumps({"command": "resume", "sender": "api", "timestamp": time.time()}),
        )
    return web.json_response({"status": "resuming"})


async def actor_metrics_handler(request: web.Request) -> Response:
    actor_id = request.match_info["actor_id"]
    ag = runtime.state["agents"].get(actor_id)
    actor = None
    if runtime.registry is not None:
        actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)
    if actor is None and ag is None:
        return web.json_response({"error": "actor not found"}, status=404)
    metrics_obj = getattr(actor, "metrics", None) if actor else None
    return web.json_response(
        {
            "messages_processed": (
                getattr(metrics_obj, "messages_processed", None)
                or (ag.get("messages_processed") if ag else None)
                or 0
            ),
            "cpu": ag.get("cpu") if ag else None,
            "mem": ag.get("mem") if ag else None,
            "task": ag.get("task") if ag else None,
            "cost_usd": (
                getattr(actor, "total_cost_usd", None) or (ag.get("cost_usd") if ag else None)
            ),
        }
    )


async def actors_handler(request: web.Request) -> Response:
    # Prefer the live registry (injected by cli.py) — actor objects carry the
    # authoritative protected flag.  Fall back to MQTT-derived state dict when
    # the registry is unavailable (standalone monitor_server mode).
    #
    # CONTRACT: the registry path intentionally excludes remote-runner agents
    # (they are not in the local Python registry).  The frontend relies on this
    # to distinguish local vs remote agents: any agent absent from this response
    # but present via MQTT heartbeat with a "node" field is a remote agent and
    # must NOT be evicted by the 15-second REST reconcile cycle.
    if runtime.registry is not None:
        result = []
        for actor in runtime.registry.all_actors():
            if runtime.is_deleted(actor.actor_id):
                continue
            ag = runtime.state["agents"].get(actor.actor_id, {})
            result.append(
                {
                    "id": actor.actor_id,
                    "name": actor.name,
                    "state": ag.get("state", "unknown"),
                    "protected": bool(getattr(actor, "protected", False)),
                    "cpu": ag.get("cpu"),
                    "mem": ag.get("mem"),
                    "task": ag.get("task"),
                    "messagesProcessed": ag.get("messages_processed")
                    if ag.get("messages_processed") is not None
                    else getattr(getattr(actor, "metrics", None), "messages_processed", None),
                    "costUsd": cost.actor_cost(actor, ag),
                }
            )
        return web.json_response(result)
    return web.json_response([_actor_payload(ag) for ag in runtime.state["agents"].values()])


async def actor_handler(request: web.Request) -> Response:
    actor_id = request.match_info["actor_id"]
    ag = runtime.state["agents"].get(actor_id)
    if ag is None:
        return web.json_response({"error": "actor not found"}, status=404)
    return web.json_response(_actor_payload(ag))


async def actor_history_handler(request: web.Request) -> Response:
    actor_id = request.match_info["actor_id"]

    # Resolve actor: the frontend sends the agent NAME (not UUID), so try
    # direct UUID lookup first, then fall back to name-based lookup.
    actor = None
    if runtime.registry is not None:
        actor = runtime.registry.get(actor_id) or runtime.registry.find_by_name(actor_id)

    if actor is not None and hasattr(actor, "recall"):
        history = actor.recall("conversation_history", [])
    elif runtime.db is not None:
        # Actor not in registry (deleted or name-only lookup) — read from SQLite.
        # actor_id might be a display name (e.g. "main") — try it directly.
        try:
            row = runtime.db.conn.execute(
                "SELECT value FROM kv_store WHERE agent=? AND key='conversation_history'",
                (actor_id,),
            ).fetchone()
            history = json.loads(row[0]) if row else []
        except Exception:
            history = []
    else:
        history = []

    visible = [m for m in history if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    return web.json_response(visible)


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
    from .config import CONFIG
    from .ext import collect_public_config

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


async def feed_handler(request: web.Request) -> Response:
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
            logger.warning(f"[feed] chat_log query failed: {exc}")
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
        logger.warning(f"[feed] handler failed: {exc}")
        return web.json_response([])


# ── Entry point ────────────────────────────────────────────────────────────


async def main(exit_on_failure: bool = False):
    # ... (startup checks remain same) ...
    mqtt_ok = await mqtt.check_mqtt()
    port_ok = await _check_ws_port()

    if not mqtt_ok or not port_ok:
        msg = []
        if not mqtt_ok:
            msg.append(f"MQTT broker unreachable ({runtime.MQTT_BROKER}:{runtime.MQTT_PORT})")
        if not port_ok:
            msg.append(f"Port {runtime.WS_PORT} already in use")
        logger.error(f"[startup] Cannot start: {'; '.join(msg)}")
        if exit_on_failure:
            raise SystemExit(1)
        return

    @web.middleware
    async def cors_middleware(request: web.Request, handler: Handler) -> Response:
        if request.method == "OPTIONS":
            return web.Response(
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization",
                }
            )
        response = await handler(request)
        try:
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        except Exception:
            pass
        return response

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", static_site.index_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/cost", cost_handler)
    app.router.add_get("/cost", cost_handler)
    app.router.add_post("/api/cost/limit", cost_limit_handler)
    app.router.add_post("/cost/limit", cost_limit_handler)
    app.router.add_post("/api/cost/reset", cost_reset_handler)
    app.router.add_post("/cost/reset", cost_reset_handler)
    app.router.add_get("/ws", ws.ws_handler)

    # Actor collection
    app.router.add_get("/api/actors", actors_handler)
    app.router.add_get("/actors", actors_handler)

    # Actor control — sub-routes must be registered before /{actor_id} catch-all
    app.router.add_post("/api/actors/{actor_id}/message", send_message_handler)
    app.router.add_post("/actors/{actor_id}/message", send_message_handler)
    app.router.add_post("/api/actors/{actor_id}/pause", pause_actor_handler)
    app.router.add_post("/actors/{actor_id}/pause", pause_actor_handler)
    app.router.add_post("/api/actors/{actor_id}/resume", resume_actor_handler)
    app.router.add_post("/actors/{actor_id}/resume", resume_actor_handler)
    app.router.add_get("/api/actors/{actor_id}/metrics", actor_metrics_handler)
    app.router.add_get("/actors/{actor_id}/metrics", actor_metrics_handler)
    app.router.add_get("/api/actors/{actor_id}/history", actor_history_handler)
    app.router.add_get("/actors/{actor_id}/history", actor_history_handler)

    # Actor CRUD
    app.router.add_get("/api/actors/{actor_id}", actor_handler)
    app.router.add_get("/actors/{actor_id}", actor_handler)
    app.router.add_delete("/api/actors/{actor_id}", delete_actor_handler)
    app.router.add_delete("/actors/{actor_id}", delete_actor_handler)

    # Chat (REST fire-and-forget)
    app.router.add_post("/api/chat", chat.rest_chat_handler)
    app.router.add_post("/chat", chat.rest_chat_handler)
    app.router.add_post("/api/chat/stop", chat.rest_chat_stop_handler)
    app.router.add_post("/chat/stop", chat.rest_chat_stop_handler)

    app.router.add_get("/api/chats", chat_log_handler)
    app.router.add_get("/chats", chat_log_handler)

    app.router.add_get("/api/config", config_handler)
    app.router.add_get("/config", config_handler)
    app.router.add_get("/api/feed", feed_handler)
    app.router.add_get("/feed", feed_handler)
    app.router.add_post("/api/reset", reset_handler)
    app.router.add_get("/favicon.svg", static_site.index_handler)

    # Extensions (wactorz/ext/): additive features register their own routes
    # and startup/teardown hooks here. Must run BEFORE the docs/static
    # catch-alls below, or the /{path:.+} route shadows extension routes.
    from .ext import setup_all

    setup_all(app)

    app.router.add_get("/docs", static_site.docs_redirect)
    app.router.add_get("/docs/", static_site.docs_handler)
    app.router.add_get("/docs/{path:.+}", static_site.docs_handler)

    app.router.add_get("/{path:.+}", static_site.static_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", runtime.WS_PORT)
    await site.start()
    logger.info(f"Monitor  → http://localhost:{runtime.WS_PORT}/  [chat: {chat.chat_mode()}]")
    if static_site.DOCS_SITE.is_dir():
        logger.info(f"Docs     → http://localhost:{runtime.WS_PORT}/docs/")

    await mqtt.mqtt_listener()


def cli_main() -> None:
    if sys.platform == "win32":
        # On Windows we manage the loop manually so paho-mqtt's __del__ doesn't
        # race against a closed loop during interpreter shutdown, which would
        # produce spurious "RuntimeError: Event loop is closed" noise from
        # aiomqtt's _on_socket_close callback.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(main(exit_on_failure=True))
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            # Cancel all pending tasks so paho gets a chance to close its
            # sockets while the loop is still alive.
            try:
                pending = asyncio.all_tasks(loop)
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            # Brief sleep lets paho's internal socket-close callback fire
            # before we seal the loop for good.
            try:
                loop.run_until_complete(asyncio.sleep(0.25))
            except Exception:
                pass
            loop.close()
    else:
        asyncio.run(main(exit_on_failure=True))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Wactorz Monitor Server")
    parser.add_argument("--broker", default=os.getenv("WACTORZ_BROKER", "localhost"))
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--mqtt-ws-port", type=int, default=int(os.getenv("MQTT_WS_PORT", "9001")))
    parser.add_argument("--ws-port", type=int, default=int(os.getenv("MONITOR_PORT", "8888")))
    args = parser.parse_args()

    this_module = sys.modules[__name__]
    this_module.MQTT_BROKER = args.broker  # pyright: ignore[reportAttributeAccessIssue]
    this_module.MQTT_PORT = args.mqtt_port  # pyright: ignore[reportAttributeAccessIssue]
    this_module.MQTT_WS_PORT = args.mqtt_ws_port  # pyright: ignore[reportAttributeAccessIssue]
    this_module.WS_PORT = args.ws_port  # pyright: ignore[reportAttributeAccessIssue]

    cli_main()


# ── Compatibility façade ──────────────────────────────────────────────────────
# The shared mutable state lives in wactorz.monitor.runtime and extracted
# helpers in their monitor.* modules. External code and tests keep using this
# module's attributes (including pre-split underscore names); forward reads AND
# writes to the real home so injection/monkeypatching is seen by every caller.
_FORWARD = {
    # runtime state (clean names)
    **{
        n: (runtime, n)
        for n in (
            "registry",
            "db",
            "state",
            "ws_clients",
            "mqtt_client_ref",
            "mqtt_connected",
            "hard_resetting",
            "deleted_agent_ids",
            "DELETED_IDS_MAX",
            "MQTT_BROKER",
            "MQTT_PORT",
            "MQTT_WS_PORT",
            "WS_PORT",
            "MQTT_TOPICS",
            "IO_GATEWAY_ID",
            "is_deleted",
            "mark_deleted",
            "undelete",
        )
    },
    # static_site module
    **{
        n: (static_site, n)
        for n in (
            "index_handler",
            "static_handler",
            "docs_handler",
            "docs_redirect",
            "csp_policy",
            "FRONTEND_DIST",
            "FRONTEND_PUBLIC",
            "DOCS_SITE",
        )
    },
    # mqtt module
    **{
        n: (mqtt, n)
        for n in ("mqtt_listener", "set_mqtt_status", "check_mqtt", "broadcast_mqtt_msg")
    },
    # ws module
    **{n: (ws, n) for n in ("broadcast", "ws_handler", "handle_command")},
    # lifecycle module
    **{
        n: (lifecycle, n)
        for n in (
            "purge_agent_retained",
            "purge_node_desired_state",
            "purge_spawn_reconcile",
            "delete_agent",
        )
    },
    # chat module
    **{
        n: (chat, n)
        for n in (
            "route_chat",
            "handle_slash",
            "handle_chat_mqtt",
            "rest_chat_handler",
            "rest_chat_stop_handler",
            "track_chat_task",
            "chat_mode",
            "find_main",
            "parse_mention",
            "slash_deploy",
            "no_op_async",
            "inflight_chat_tasks",
        )
    },
    # cost module
    **{
        n: (cost, n)
        for n in (
            "ensure_lifetime_loaded",
            "record_lifetime_cost",
            "lifetime_cost_total",
            "reset_actor_cost",
            "historical_cost_usd",
            "historical_messages",
            "actor_cost",
            "best_cost",
            "best_msgs",
            "lifetime_cost",
            "lifetime_loaded",
            "LIFETIME_LEDGER_KEY",
        )
    },
    # events module (clean names)
    **{
        n: (events, n)
        for n in (
            "parse_topic",
            "update_agent",
            "add_log",
            "snapshot",
            "node_online",
        )
    },
}


def __getattr__(name: str):
    target = _FORWARD.get(name)
    if target is not None:
        return getattr(target[0], target[1])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _FacadeModule(type(sys.modules[__name__])):
    def __setattr__(self, name: str, value) -> None:
        target = _FORWARD.get(name)
        if target is not None:
            setattr(target[0], target[1], value)
        else:
            super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule
