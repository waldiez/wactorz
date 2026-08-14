"""The reset endpoint and its factory-reset keep-set.

``POST /api/reset`` supports several scopes (chat, metrics, spawns, state, all).
The ``all`` scope is a factory reset: it stops and forgets user-spawned agents
while keeping the ones a fresh boot would recreate — the protected actors plus
the Home Assistant system agents in ``HA_SYSTEM_AGENTS``.
"""

import asyncio
import json
import logging
from typing import Any

from aiohttp import web
from aiohttp.web import Response

from ..agents.lookup import find_main_actor
from . import cost, events, lifecycle, runtime, ws

logger = logging.getLogger(__name__)

# Home-Assistant system agents: supervised on a fresh boot like the protected
# actors, but intentionally left NON-protected so a user can still delete one
# individually. A factory reset must keep them anyway (a clean boot recreates
# them), so they are named explicitly here rather than inferred from protection.
HA_SYSTEM_AGENTS = frozenset(
    {
        "home-assistant-agent",
        "home-assistant-map-agent",
        "home-assistant-state-bridge",
    }
)


#: The legacy-pickle keys a *chat* reset owns. Mirrors `reset._CHAT_KV_KEYS`,
#: which is what the same reset deletes from the database.
CHAT_STATE_KEYS = ("conversation_history", "history_summary")


def forget_legacy_state(actor: Any, keys: tuple[str, ...] | None = None) -> None:
    """Drop the legacy pickle copy a wipe cannot otherwise reach.

    `Actor._load_persistent_state` loads a legacy `state.pkl` into
    `_persistent_state` even on the new store path, and `Actor.recall` falls
    back to that dict whenever the new store returns `None`. A reset empties the
    store and deletes the file, so `recall` then finds nothing, falls through,
    and returns the very value that was just destroyed — for as long as the
    process lives. On the legacy write path the next `persist` puts the file
    back from the same dict.

    `keys=None` clears the lot, for a factory reset: it has already deleted the
    pickle from disk, so anything left in memory is the two halves disagreeing.
    A chat reset passes `CHAT_STATE_KEYS` instead, because it clears the
    conversation and nothing else.
    """
    state = getattr(actor, "_persistent_state", None)
    if not isinstance(state, dict):
        return
    if keys is None:
        state.clear()
        return
    for key in keys:
        state.pop(key, None)


def survives_factory_reset(name: str, protected: bool) -> bool:
    """Whether an agent is kept by ``reset all`` (factory reset).

    Kept if it is a system-protected actor (main / monitor / io-agent / installer
    / catalog) OR one of the HA system agents — i.e. exactly the set a fresh,
    empty boot brings up. Everything else (user- and catalog-spawned agents) is
    wiped. Note this is independent of manual delete, which keys off ``protected``
    alone, so the HA agents stay individually deletable.
    """
    return protected or name in HA_SYSTEM_AGENTS


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
            main_actor = find_main_actor(runtime.registry)
            if main_actor is not None:
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
                    actor._conversation_history = []  # pyright: ignore[reportAttributeAccessIssue]
                if hasattr(actor, "_history_summary"):
                    actor._history_summary = ""  # pyright: ignore[reportAttributeAccessIssue]
                if hasattr(actor, "_user_facts"):
                    actor._user_facts = {}  # pyright: ignore[reportAttributeAccessIssue]
                if hasattr(actor, "_pipeline_rules"):
                    actor._pipeline_rules = []  # pyright: ignore[reportAttributeAccessIssue]
                # The attributes above are the live copies; this is the one
                # behind them. Without it `recall` keeps answering from the
                # pickle this wipe deleted, and every reset looks like it failed.
                forget_legacy_state(actor)
                # NOTE: the kv-backed spawn registry is cleared by
                # _purge_spawn_reconcile() above — assigning the _spawned_agents
                # attribute was a no-op (the registry is read via recall()).
            _reset.reset_all(agent)
            # Clear the in-memory lifetime cost ledger + global accumulator too, or
            # the headline total re-pins to the old high-water once _hard_resetting
            # clears and heartbeats resume (mirrors /api/cost/reset).
            cost.lifetime_cost.clear()
            try:
                from ..agents.llm_agent import reset_global_cost

                reset_global_cost()
            except Exception as exc:
                logger.debug("[reset] reset_global_cost skipped: %s", exc)
            runtime.state["agents"].clear()
            runtime.state["nodes"].clear()
            runtime.state["alerts"].clear()
            runtime.state["log_feed"].clear()
        finally:
            runtime.hard_resetting = False

        # The kept agents were never stopped — only forgotten, because the state
        # above was cleared. The registry knows them all, in memory, now — so
        # rebuild from it and send ONE frame whose state already carries the
        # survivors. Both calls must follow the flag clearing: `update_agent`
        # drops writes and `snapshot` returns empty lists while a hard reset is
        # in progress.
        restored = events.rebuild_from_registry(runtime.registry)
        if restored:
            logger.info("[reset] restored %d running agent(s) to the dashboard", restored)
        await ws.broadcast(
            {
                "type": "reset",
                "scope": "all",
                "agent": None,
                "state": events.snapshot(),
            }
        )
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
                actor._conversation_history = []  # pyright: ignore[reportAttributeAccessIssue]
            if hasattr(actor, "_history_summary"):
                actor._history_summary = ""  # pyright: ignore[reportAttributeAccessIssue]
            # Only the conversation keys: a chat reset is not a factory reset,
            # and the rest of the legacy dict is not its to drop.
            forget_legacy_state(actor, CHAT_STATE_KEYS)
    elif scope == "state":
        if agent:
            _reset.reset_agent_state(agent)
        else:
            _reset._reset_all_pickles()
        # The files are gone; the copies loaded from them are not.
        live_actors = list(runtime.registry.all_actors()) if runtime.registry is not None else []
        for actor in live_actors:
            if agent and actor.name != agent:
                continue
            forget_legacy_state(actor)
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
                from ..agents.llm_agent import reset_global_cost

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
