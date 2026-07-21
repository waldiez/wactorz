"""MQTT-topic → live-state transitions.

``parse_topic`` decodes each broker message into mutations of
``runtime.state`` (agents, nodes, alerts, log feed); ``snapshot`` renders
that state for a newly connected websocket client.
"""

import json
import logging
import time
from typing import Any

from . import runtime

logger = logging.getLogger(__name__)


def _ms():
    """The monitor façade — for helpers not yet extracted from it.

    Looked up lazily at call time so tests that monkeypatch them on
    ``wactorz.monitor_server`` keep working, and no import cycle forms.
    """
    import wactorz.monitor_server as ms

    return ms


def update_agent(agent_id: str, key: str, data) -> None:
    if runtime.hard_resetting or runtime.is_deleted(agent_id):
        return
    if agent_id not in runtime.state["agents"]:
        runtime.state["agents"][agent_id] = {
            "agent_id": agent_id,
            "name": agent_id[:8],
            "first_seen": time.time(),
        }
    runtime.state["agents"][agent_id][key] = data
    runtime.state["agents"][agent_id]["last_update"] = time.time()


def add_log(entry: dict) -> None:
    runtime.state["log_feed"].insert(0, entry)
    if len(runtime.state["log_feed"]) > 100:
        runtime.state["log_feed"].pop()


def parse_topic(topic: str, payload_str: str) -> dict[str, Any] | None:
    try:
        data = json.loads(payload_str)
    except Exception:  # pylint: disable=broad-exception-caught
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
            update_agent(agent_id, "heartbeat", data)
            if isinstance(data, dict) and agent_id in runtime.state["agents"]:
                ag = runtime.state["agents"][agent_id]
                ag["name"] = data.get("name", agent_id[:8])
                ag["cpu"] = data.get("cpu", 0)
                ag["mem"] = data.get("memory_mb", 0)
                ag["task"] = data.get("task", "idle")
                ag["state"] = data.get("state", "unknown")
                # Remote agents' heartbeats include "node" — capture it so the
                # dashboard delete path can route the stop to the right runner.
                # Local agents don't set this field; absence means "local".
                if data.get("node"):
                    ag["node"] = data["node"]
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
                    _ms().record_lifetime_cost(agent_id, data.get("cost_usd"))

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
    return (time.time() - last_seen) < threshold


def snapshot() -> dict[str, Any]:
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
        live_cost += _ms().best_cost(ag, actor, name)
        live_msgs += _ms().best_msgs(ag, actor)
    # Fold in live actors not yet in state (the post-restart window before the
    # first heartbeat). Keyed by actor_id so agents already counted above are
    # skipped — no double-count.
    for a in actors_by_id.values():
        if a.actor_id in seen_ids:
            continue
        live_names.add(a.name)
        live_cost += _ms().best_cost(None, a, a.name)
        live_msgs += _ms().best_msgs(None, a)

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
    except Exception:  # pylint: disable=broad-exception-caught
        alltime_cost = 0.0
    total_cost = max(
        live_cost + _ms().historical_cost_usd(live_names),
        _ms().lifetime_cost_total(),
        alltime_cost,
    )
    total_msgs = live_msgs + _ms().historical_messages(live_names)
    return {
        "agents": list(runtime.state["agents"].values()),
        "nodes": list(runtime.state["nodes"].values()),
        "alerts": runtime.state["alerts"][:10],
        "log_feed": runtime.state["log_feed"][:20],
        "system_health": runtime.state["system_health"],
        "total_cost_usd": round(total_cost, 6),
        "total_messages": total_msgs,
    }
