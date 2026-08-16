"""MQTT-topic → live-state transitions.

``parse_topic`` decodes each broker message into mutations of
``runtime.state`` (agents, nodes, alerts, log feed); ``snapshot`` renders
that state for a newly connected websocket client.
"""

import json
import logging
import time
from typing import Any

from . import cost, runtime

logger = logging.getLogger(__name__)


#: Most agents tracked in live state at once. Entries are created on any
#: `agents/<id>/…` publish, and only an explicit delete or reset removes them —
#: so a client publishing under fresh ids grows this map for as long as it keeps
#: publishing. Well above any real deployment; it is a ceiling, not a budget.
MAX_TRACKED_AGENTS = 500

#: Frame type announcing that an agent is gone. The dashboard matches this
#: string exactly to drop the card and tombstone the id; a state snapshot alone
#: will not remove it, because a patch only adds and updates. Kept here so the
#: REST and WebSocket delete paths cannot spell it differently.
DELETE_AGENT_FRAME = "delete_agent"


def _evict_stalest() -> None:
    """Drop the least recently updated agents once the map is over its ceiling.

    Eviction is by staleness rather than insertion order because a real agent
    heartbeats: it keeps refreshing `last_update` and so is never the oldest,
    while entries invented by a flood go stale immediately and are dropped
    first. Evicting by age alone would let the flood push out live agents.
    """
    agents = runtime.state["agents"]
    overflow = len(agents) - MAX_TRACKED_AGENTS
    if overflow <= 0:
        return
    stalest = sorted(agents, key=lambda aid: agents[aid].get("last_update", 0))[:overflow]
    for aid in stalest:
        agents.pop(aid, None)
    logger.warning(
        "[events] Live agent map hit %d; dropped the %d stalest. "
        "Expected only under a flood of unknown agent ids.",
        MAX_TRACKED_AGENTS,
        overflow,
    )


def update_agent(agent_id: str, key: str, data) -> None:
    """Merge one field of an agent's live state, re-admitting it if respawned."""
    if runtime.hard_resetting or runtime.is_deleted(agent_id):
        return
    if agent_id not in runtime.state["agents"]:
        runtime.state["agents"][agent_id] = {
            "agent_id": agent_id,
            "name": agent_id[:8],
            "first_seen": time.time(),
            # Set here as well as below: eviction ranks on it, and an entry
            # without one sorts as infinitely stale — so a new arrival would be
            # the first thing dropped, including itself.
            "last_update": time.time(),
        }
        _evict_stalest()
    runtime.state["agents"][agent_id][key] = data
    runtime.state["agents"][agent_id]["last_update"] = time.time()


def record_heartbeat(agent_id: str, data: Any) -> None:
    """Fold one heartbeat payload into an agent's dashboard entry.

    Shared by the broker listener and the post-reset rebuild so the two cannot
    describe an agent differently. The payload is whatever `Actor._build_heartbeat`
    produces, which is what arrives over MQTT.
    """
    update_agent(agent_id, "heartbeat", data)
    if not isinstance(data, dict) or agent_id not in runtime.state["agents"]:
        return
    ag = runtime.state["agents"][agent_id]
    ag["name"] = data.get("name", agent_id[:8])
    ag["cpu"] = data.get("cpu", 0)
    ag["mem"] = data.get("memory_mb", 0)
    ag["task"] = data.get("task", "idle")
    ag["state"] = data.get("state", "unknown")
    # Heartbeats have always carried this; nothing copied it out, so an entry's
    # `protected` was set only by whichever other path happened to run first.
    # `reset all` keeps an agent when it is protected OR an HA system agent, so
    # a missing flag makes a protected agent look disposable — which is what
    # api_reset's own comment warns about when it distrusts this field.
    if "protected" in data:
        ag["protected"] = bool(data["protected"])
    # Remote agents' heartbeats include "node" — capture it so the dashboard
    # delete path can route the stop to the right runner. Local agents don't set
    # this field; absence means "local".
    if data.get("node"):
        ag["node"] = data["node"]


def rebuild_from_registry(registry: Any) -> int:
    """Repopulate the dashboard's agent list from the actors that are running.

    A reset clears `state["agents"]` but does not stop the agents it keeps, so
    the dashboard forgets actors that are alive and cannot ask about them — the
    list only refills as each one next heartbeats, up to a full interval later.
    The registry is authoritative and in memory, so it can answer immediately.

    Returns how many agents were restored.
    """
    if registry is None:
        return 0
    restored = 0
    for actor in registry.all_actors():
        try:
            record_heartbeat(actor.actor_id, actor._build_heartbeat())
            restored += 1
        except Exception as exc:
            # One unhappy actor must not leave the rest of the list missing.
            logger.warning("[reset] could not restore %s: %s", getattr(actor, "name", "?"), exc)
    return restored


def add_log(entry: dict) -> None:
    """Append to the bounded log feed shared with connected browsers.

    Stamps ``source`` here rather than at each of the call sites, so a later one
    cannot forget it. The value is a constant telling the feed view which kind of
    entry it is holding.

    Assigned, not ``setdefault``: two call sites spread ``**data`` from the
    broker payload into the entry, so a publisher could otherwise label its own
    row ``app`` and have it render as an application log line. Everything
    reaching this function is agent activity by definition — application log
    records go to the in-memory buffer, never here.
    """
    entry["source"] = "agent"
    runtime.state["log_feed"].insert(0, entry)
    if len(runtime.state["log_feed"]) > 100:
        runtime.state["log_feed"].pop()


def parse_topic(topic: str, payload_str: str) -> dict[str, Any] | None:
    """Decode one broker message into a state mutation.

    Returns the event dict to broadcast to browsers, or ``None`` when the
    message needs no client-side notification.
    """
    try:
        data = json.loads(payload_str)
    except Exception:
        data = payload_str

    parts = topic.split("/")

    if parts[0] == "system" and len(parts) >= 2:
        if parts[1] == "health":
            runtime.state["system_health"] = data
        elif parts[1] == "alerts":
            runtime.state["alerts"].insert(0, data)
            if len(runtime.state["alerts"]) > 50:
                runtime.state["alerts"].pop()
        return {"type": "system", "subtype": parts[1], "data": data}

    if parts[0] == "agents" and len(parts) >= 3:
        agent_id = parts[1]
        metric = parts[2]

        # Re-admit a deleted agent on a FRESH status event. Every actor
        # publishes its first status from on_start(), with uptime ≈ 0; that's
        # the unambiguous "I just started" signal. A stale retained status
        # from the previous (deleted) incarnation would carry the uptime it
        # had at the moment of deletion (typically large), so we don't
        # confuse it with a respawn.
        #
        # Without this, deleting an agent and respawning it under the same
        # name produces the same deterministic actor_id (uuid5 of the name),
        # the deleted guard fires, and the new instance is invisible in the
        # dashboard even though it's actually running.
        if metric == "status" and isinstance(data, dict) and runtime.is_deleted(agent_id):
            uptime = data.get("uptime", 0)
            try:
                uptime = float(uptime)
            except (TypeError, ValueError):
                uptime = 0.0
            agent_state = data.get("state", "")
            if uptime < 10.0 and agent_state not in ("stopped", "failed"):
                runtime.undelete(agent_id)
                msg = (
                    f"[MQTT] Re-admitting respawned agent {agent_id[:8]} "
                    f"(uptime={uptime:.1f}s, state={agent_state}, previously deleted)"
                )
                logger.info(msg)

        # If the agent was just deleted, update_agent() refuses to recreate
        # the entry — so any direct state["agents"][agent_id] access below
        # would KeyError. Skip the whole branch; the agent is gone.
        if runtime.is_deleted(agent_id):
            return {"type": "agent", "subtype": metric, "agent_id": agent_id, "data": data}

        if metric == "status":
            update_agent(agent_id, "status", data)
            if isinstance(data, dict) and agent_id in runtime.state["agents"]:
                if "name" in data:
                    runtime.state["agents"][agent_id]["name"] = data["name"]
                if "state" in data:
                    runtime.state["agents"][agent_id]["state"] = data["state"]
                if "protected" in data:
                    runtime.state["agents"][agent_id]["protected"] = data["protected"]
            name = runtime.state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            add_log(
                {
                    "type": "status",
                    "agent_id": agent_id,
                    "name": name,
                    "status": data,
                    "timestamp": time.time(),
                }
            )

        elif metric == "heartbeat":
            record_heartbeat(agent_id, data)
            if agent_id in runtime.state["agents"]:
                agent_name = runtime.state["agents"][agent_id].get("name", agent_id[:8])
                msg = f"[MQTT] Heartbeat: {agent_name}"
                logger.info(msg)

        elif metric == "metrics":
            update_agent(agent_id, "metrics", data)
            if isinstance(data, dict) and agent_id in runtime.state["agents"]:
                runtime.state["agents"][agent_id]["messages_processed"] = data.get(
                    "messages_processed", 0
                )
                if "cost_usd" in data:
                    runtime.state["agents"][agent_id]["cost_usd"] = data.get("cost_usd", 0.0)
                    runtime.state["agents"][agent_id]["input_tokens"] = data.get("input_tokens", 0)
                    runtime.state["agents"][agent_id]["output_tokens"] = data.get(
                        "output_tokens", 0
                    )
                    # Bank the spend durably so it survives the agent being
                    # deleted or hard-killed before its on_stop() can persist.
                    cost.record_lifetime_cost(agent_id, data.get("cost_usd"))

        elif metric == "logs":
            # Log frames carry only the agent id; resolve the friendly name the
            # same way alert/completed do so the feed never shows a bare id.
            # `**data` last lets a payload that already includes a name win.
            name = runtime.state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            add_log(
                {
                    "type": "log",
                    "agent_id": agent_id,
                    "name": name,
                    "timestamp": time.time(),
                    **(data if isinstance(data, dict) else {}),
                }
            )
        elif metric == "spawned":
            # Payload carries child_name/child_id, not name — resolve the (parent)
            # agent's name from state so the feed row isn't attributed to a bare id.
            name = runtime.state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            add_log(
                {
                    "type": "spawned",
                    "agent_id": agent_id,
                    "name": name,
                    "timestamp": time.time(),
                    **(data if isinstance(data, dict) else {}),
                }
            )
        elif metric == "chat":
            # User-facing message pushed by an agent via Actor.notify_user().
            # Forward it to the chat panel as a live chat frame (in addition to
            # the dashboard feed). The frame is carried under "_push_chat";
            # mqtt_listener does the broadcast since parse_topic is synchronous.
            sender = runtime.state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            content = ""
            if isinstance(data, dict):
                content = (data.get("content") or data.get("text") or "").strip()
                sender = data.get("from") or sender
            elif isinstance(data, str):
                content = data.strip()
            if content:
                add_log(
                    {
                        "type": "chat",
                        "agent_id": agent_id,
                        "from": sender,
                        "content": content,
                        "timestamp": time.time(),
                    }
                )
                return {
                    "type": "agent",
                    "agent_id": agent_id,
                    "metric": "chat",
                    "data": data,
                    "_push_chat": {
                        "type": "chat",
                        "from": sender,
                        "content": content,
                        "timestamp": time.time(),
                    },
                }
            return {"type": "agent", "agent_id": agent_id, "metric": "chat", "data": data}
        elif metric == "completed":
            update_agent(agent_id, "last_completed", data)
            name = runtime.state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            add_log(
                {"type": "completed", "agent_id": agent_id, "name": name, "timestamp": time.time()}
            )
        elif metric == "alert":
            if isinstance(data, dict):
                data["agent_id"] = agent_id
                data.setdefault(
                    "name", runtime.state["agents"].get(agent_id, {}).get("name", agent_id[:8])
                )
            runtime.state["alerts"].insert(
                0, data if isinstance(data, dict) else {"agent_id": agent_id}
            )
            if len(runtime.state["alerts"]) > 50:
                runtime.state["alerts"].pop()
            name = runtime.state["agents"].get(agent_id, {}).get("name", agent_id[:8])
            severity = data.get("severity", "warning") if isinstance(data, dict) else "warning"
            add_log(
                {
                    "type": "alert",
                    "agent_id": agent_id,
                    "name": name,
                    "message": f"{name} unresponsive ({severity})",
                    "timestamp": time.time(),
                }
            )

        return {"type": "agent", "agent_id": agent_id, "metric": metric, "data": data}

    if parts[0] == "nodes" and len(parts) >= 3 and parts[2] == "heartbeat":
        node_name = parts[1]
        if isinstance(data, dict):
            runtime.state["nodes"][node_name] = {
                "node": node_name,
                "agents": data.get("agents", []),
                "last_seen": time.time(),
                "online": True,
                "node_id": data.get("node_id", ""),
            }
            logger.info("[MQTT] Node heartbeat: %s | agents: %s", node_name, data.get("agents", []))
            return {"type": "node", "node_name": node_name, "data": data}

    return None


def node_online(last_seen: float, threshold: float = 45.0) -> bool:
    """True if a remote node's last heartbeat is recent enough to count as up."""
    return (time.time() - last_seen) < threshold


def snapshot(include_totals: bool = True) -> dict[str, Any]:
    """Render the dashboard state for a websocket client.

    Everything here reads from ``runtime.state`` — in memory, cheap — **except**
    the two headline totals, which are the only part that touches the database.
    Resolving them costs one query per agent whose cost is not in an MQTT frame
    (``best_cost`` falls through to the ``_final_cost`` row), plus two full scans
    of ``kv_store``.

    ``include_totals=False`` omits them, for callers on a hot path. The browser
    keeps whatever it last received: ``WSClient._applyStatePatch`` assigns each
    total only when the key is present, so omitting them is not the same as
    sending zero, and no protocol or frontend change is involved.
    """
    if runtime.hard_resetting:
        return {
            "agents": [],
            "nodes": [],
            "alerts": [],
            "log_feed": [],
            "total_cost_usd": 0,
            "total_messages": 0,
        }
    for nd in runtime.state["nodes"].values():
        nd["online"] = node_online(nd.get("last_seen", 0))

    if not include_totals:
        return {
            "agents": list(runtime.state["agents"].values()),
            "nodes": list(runtime.state["nodes"].values()),
            "alerts": runtime.state["alerts"][:10],
            "log_feed": runtime.state["log_feed"][:20],
            "system_health": runtime.state["system_health"],
        }

    # The headline totals must match what the dashboard actually shows on the
    # cards. Each card resolves its cost via _actor_cost() — MQTT state, then the
    # live actor object, then the persisted _final_cost row — so the header has to
    # coalesce the same three sources per agent. Summing only state["cost_usd"]
    # (or only iterating the local registry) dropped any on-screen agent whose
    # cost lives on the actor object / SQLite rather than in an MQTT metrics frame.
    actors_by_id: dict = {}
    actors_by_name: dict = {}
    if runtime.registry is not None:
        for a in runtime.registry.all_actors():
            actors_by_id[a.actor_id] = a
            actors_by_name[a.name] = a

    live_names: set = set()
    live_cost = 0.0
    live_msgs = 0
    seen_ids: set = set()
    for aid, ag in runtime.state["agents"].items():
        seen_ids.add(aid)
        name = ag.get("name", "")
        live_names.add(name)
        actor = actors_by_id.get(aid) or actors_by_name.get(name)
        live_cost += cost.best_cost(ag, actor, name)
        live_msgs += cost.best_msgs(ag, actor)
    # Fold in live actors not yet in state (the post-restart window before the
    # first heartbeat). Keyed by actor_id so agents already counted above are
    # skipped — no double-count.
    for a in actors_by_id.values():
        if a.actor_id in seen_ids:
            continue
        live_names.add(a.name)
        live_cost += cost.best_cost(None, a, a.name)
        live_msgs += cost.best_msgs(None, a)

    # The live + _final_cost sum can dip when an agent is deleted (its _final_cost
    # row is purged) or hard-killed (its on_stop never ran). The durable ledger is
    # monotonic, so clamp the headline total up to whichever is larger — spend is
    # never lost, and the live path still covers the fresh-boot window before the
    # first heartbeat repopulates the ledger.
    # The all-time call-time counter is delete-proof (a deleted agent's _final_cost
    # row is purged and its lifetime-ledger high-water can be missed/popped, but
    # the counter accrued its spend at call time). Use it as a third floor so the
    # headline never drops below money already spent — and so it can never read
    # lower than the "this period" spend shown beside it.
    try:
        from ..agents.llm_agent import get_global_alltime_cost

        alltime_cost = get_global_alltime_cost()
    except Exception:
        alltime_cost = 0.0
    total_cost = max(
        live_cost + cost.historical_cost_usd(live_names),
        cost.lifetime_cost_total(),
        alltime_cost,
    )
    total_msgs = live_msgs + cost.historical_messages(live_names)
    return {
        "agents": list(runtime.state["agents"].values()),
        "nodes": list(runtime.state["nodes"].values()),
        "alerts": runtime.state["alerts"][:10],
        "log_feed": runtime.state["log_feed"][:20],
        "system_health": runtime.state["system_health"],
        "total_cost_usd": round(total_cost, 6),
        "total_messages": total_msgs,
    }
