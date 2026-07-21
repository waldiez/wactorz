"""Agent lifecycle: retained-state purges and deletion.

Deleting an agent has to clear every place its state lingers — retained MQTT
topics, a node's desired-state, the spawn registry — or the monitor resurrects
it from stale broker messages on the next heartbeat.
"""

import asyncio
import json
import logging
import time

from . import chat, runtime

logger = logging.getLogger(__name__)


async def purge_agent_retained(agent_id: str) -> None:
    """Clear retained MQTT messages for a deleted agent so the broker stops
    re-delivering them after a monitor reconnect or a fresh subscribe.

    Without this, every reconnect re-fires the agent's retained status /
    heartbeat / metrics, each of which would otherwise crash parse_topic
    with KeyError on an entry that update_agent now refuses to recreate.
    """
    if not runtime.mqtt_client_ref:
        return
    for metric in (
        "status",
        "heartbeat",
        "metrics",
        "logs",
        "spawned",
        "manifest",
        "errors",
        "detections",
        "results",
        "completed",
    ):
        topic = f"agents/{agent_id}/{metric}"
        try:
            await runtime.mqtt_client_ref.publish(topic, b"", retain=True)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.debug("[purge] Failed to clear retained %s: %s", topic, e)


async def purge_node_desired_state(node: str) -> None:
    """Clear the retained nodes/{node}/desired_state message.

    The remote runner subscribes to this topic and, on reconnect/reboot,
    reconciles it by spawning any listed agent that isn't already running.
    If a reset doesn't clear it, the runner re-spawns the just-deleted agents.
    """
    if not runtime.mqtt_client_ref or not node:
        return
    topic = f"nodes/{node}/desired_state"
    try:
        await runtime.mqtt_client_ref.publish(topic, b"", retain=True)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.debug("[purge] Failed to clear retained %s: %s", topic, e)


async def purge_spawn_reconcile(agent: str | None = None) -> None:
    """Tear down the agent-respawn state behind a spawn-registry clear.

    Shared by the "spawns" and "all" reset scopes so both behave identically:
      1. clear the kv-backed registry through the live main actor — an uncached
         recall("_spawned_agents") then returns empty and a later checkpoint
         can't rewrite the cleared set back to disk; and
      2. fix the retained nodes/{node}/desired_state so a reconnecting runner
         can't reconcile cleared agents back.

    For a global clear (agent is None) every affected node's desired_state is
    blanked. For a single-agent clear it is REPUBLISHED from the reduced registry
    so sibling agents still on that node survive.

    Must run BEFORE reset_spawns()/reset_all() wipe the kv on disk — it reads the
    registry to learn which nodes are affected.
    """
    main_actor = chat.find_main()
    reg = {}
    if main_actor is not None and hasattr(main_actor, "_get_spawn_registry"):
        reg = main_actor._get_spawn_registry() or {}  # pylint: disable=protected-access

    # Affected nodes: live heartbeats ∪ registry (covers offline nodes).
    node_names: set[str] = set(runtime.state["nodes"].keys())
    for name, cfg in reg.items():
        if agent and name != agent:
            continue
        n = (cfg.get("node") or "").strip()
        if n:
            node_names.add(n)

    # Clear the live registry through the actor's own persistence.
    if (
        main_actor is not None
        and hasattr(main_actor, "recall")
        and main_actor.recall("_spawned_agents", None) is not None
    ):
        kept = {k: v for k, v in reg.items() if k != agent} if agent else {}
        main_actor.persist("_spawned_agents", kept)

    if agent and main_actor is not None and hasattr(main_actor, "_update_node_desired_state"):
        # Republish from the reduced registry so siblings on the node survive.
        await asyncio.gather(
            *[main_actor._update_node_desired_state(n) for n in node_names],  # pylint: disable=protected-access
            return_exceptions=True,
        )
    else:
        await asyncio.gather(
            *[purge_node_desired_state(n) for n in node_names],
            return_exceptions=True,
        )


async def delete_agent(agent_id: str) -> str:
    """Delete an agent properly regardless of whether it lives locally on this
    process or on a remote node. Returns a short status string for logs.

    Strategy:
      1. Mark the actor_id deleted and pop the dashboard entry.
      2. Try to route through main.delete_spawned_agent(name) — it owns the
         spawn registry, knows the agent's node, updates desired_state, sends
         the right MQTT stop signal, and clears the manifest. This is the
         canonical path; it handles local + remote uniformly.
      3. Fall back to direct MQTT if the registry isn't available (the monitor
         is running in a separate process / MQTT-only mode):
           - Remote → publish to nodes/<node>/stop using the node we captured
             from heartbeats.
           - Local → publish to agents/<id>/commands {"command": "stop"}.
      4. Clear retained messages so old heartbeats don't come back on reconnect.
    """
    record = runtime.state["agents"].get(agent_id) or {}
    name = record.get("name") or agent_id
    node = (record.get("node") or "").strip()

    runtime.mark_deleted(agent_id)
    runtime.state["agents"].pop(agent_id, None)

    routed = "unknown"

    if runtime.registry is not None:
        # In-process: delegate to main, which owns the spawn registry and
        # knows exactly how to clean up both local and remote agents.
        main_actor = chat.find_main()
        if main_actor is not None and hasattr(main_actor, "delete_spawned_agent"):
            try:
                await main_actor.delete_spawned_agent(name)
                routed = f"via main.delete_spawned_agent({name!r})"
            except Exception as e:  # pylint: disable=broad-exception-caught
                msg = (
                    f"[delete] main.delete_spawned_agent('{name}') failed: {e}; "
                    "falling back to direct MQTT"
                )
                logger.warning(msg)
                routed = "main path failed"

        # If main wasn't reachable or the call failed, also try to stop a
        # purely local actor through the registry directly. Useful for agents
        # that exist in the registry but aren't in main's spawn registry yet
        # (race window during startup).
        if routed.startswith("via main") is False:
            actor = runtime.registry.get(agent_id) or runtime.registry.find_by_name(name)
            if actor is not None and not getattr(actor, "protected", False):
                asyncio.create_task(actor.stop())
                routed = "via local registry"

    if routed in ("unknown", "main path failed") and runtime.mqtt_client_ref:
        # MQTT-only mode (or main unavailable). Route by node if we have one.
        # pylint: disable=broad-exception-caught
        if node:
            try:
                await runtime.mqtt_client_ref.publish(
                    f"nodes/{node}/stop",
                    json.dumps({"name": name}),
                )
                routed = f"via nodes/{node}/stop"
            except Exception as e:
                logger.warning("[delete] nodes/%s/stop publish failed: %s", node, e)
        else:
            try:
                await runtime.mqtt_client_ref.publish(
                    f"agents/{agent_id}/commands",
                    json.dumps({"command": "stop", "sender": "monitor", "timestamp": time.time()}),
                )
                routed = f"via agents/{agent_id}/commands"
            except Exception as e:
                logger.warning("[delete] commands publish failed: %s", e)

    # Always purge retained — even when main handled the delete, we want the
    # dashboard's view to clear immediately rather than wait for tombstones.
    asyncio.create_task(purge_agent_retained(agent_id))

    msg = f"[delete] '{name}' (id={agent_id[:8]}, node={node or 'local'}) {routed}"
    logger.info(msg)
    return routed
