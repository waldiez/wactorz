"""The reset endpoint and its factory-reset keep-set.

``POST /api/reset`` supports several scopes (chat, metrics, spawns, state, all).
The ``all`` scope is a factory reset: it stops and forgets user-spawned agents
while keeping the ones a fresh boot would recreate — the protected actors plus
the Home Assistant system agents in ``HA_SYSTEM_AGENTS``.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterator
from typing import Any

from aiohttp import web
from aiohttp.web import Response

from ..agents.llm import cost as llm_cost
from ..agents.lookup import find_main_actor
from ..core.addresses import MAIN_NODE, SEPARATOR, Address, Resolution, parse
from ..core.agent_reset import (
    LIVE_SCOPES,
    forget_conversation,
    forget_legacy_state,
    reset_actor_cost,
    reset_live,
)
from ..core.node_signing import signed_publish_kwargs
from . import cost, events, lifecycle, node_reset, runtime, ws

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _withdrawals_suppressed(main_actor: Any) -> Iterator[None]:
    """Stop main acting on the manifest withdrawals a reset itself publishes.

    A no-op when main is not in this process. That is not the same as safe: a
    main running elsewhere would see these purges over the broker with nothing
    suppressing them. The registry drops would settle correctly — they are
    idempotent, against entries the reset is clearing anyway — but it would
    record a deletion note per wiped agent. No topology splits them today; if
    one ever does, the suppression has to travel with the purge.
    """
    lifecycle_service = getattr(main_actor, "lifecycle", None)
    if lifecycle_service is None:
        yield
        return
    with lifecycle_service.withdrawals_suppressed():
        yield


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


async def forget_actor(actor: Any) -> None:
    """Stop an actor a factory reset is forgetting, after its own clean-up.

    The reset removes the agent for good, as a delete does, and purges what it
    knows about: the agent's retained `agents/<id>/` topics, its pickle, its
    spawn entry. What an agent keeps elsewhere — files of its own, retained
    messages under its own topics — only the agent knows, and `on_delete` is
    where it removes them. Stopped without it, those outlive the reset, and a
    fresh spawn of the same agent picks them up again.
    """
    await actor.delete_own_traces()
    await actor.stop()


def survives_factory_reset(name: str, protected: bool) -> bool:
    """Whether an agent is kept by ``reset all`` (factory reset).

    Kept if it is a system-protected actor (main / monitor / installer
    / catalog) OR one of the HA system agents — i.e. exactly the set a fresh,
    empty boot brings up. Everything else (user- and catalog-spawned agents) is
    wiped. Note this is independent of manual delete, which keys off ``protected``
    alone, so the HA agents stay individually deletable.
    """
    return protected or name in HA_SYSTEM_AGENTS


def _agents_on_nodes(main_actor: Any) -> dict[str, set[str]]:
    """The agents each node runs for this install, by node.

    From everything that says so: main's spawn registry, which covers a node
    that is away; the dashboard's agents; and each node's last heartbeat, which
    covers an agent the other two never heard of -- as main took it, and as the
    dashboard was shown it, which can lag main's by a heartbeat.
    """
    on: dict[str, set[str]] = {}
    registry = main_actor._get_spawn_registry() if main_actor is not None else None
    for name, config in (registry or {}).items():
        node = (config.get("node") or "").strip()
        if node:
            on.setdefault(node, set()).add(name)
    for agent in runtime.state["agents"].values():
        if agent.get("node") and agent.get("name"):
            on.setdefault(agent["node"], set()).add(agent["name"])
    heard = main_actor._known_nodes if main_actor is not None else {}
    for node, listed in [*heard.items(), *runtime.state["nodes"].items()]:
        names = {str(n) for n in listed.get("agents") or [] if n}
        on.setdefault(node, set()).update(names)
    return on


async def _delete_on_nodes(on: dict[str, set[str]]) -> None:
    """Have each node delete its agents, state and retained topics included.

    At QoS 1, so a node that is away is told when it returns. One stop per
    agent rather than ``stop_all``, which shuts the node itself down.
    """
    client = runtime.mqtt_client_ref
    if client is None:
        return
    sends = []
    for node, names in on.items():
        for name in sorted(names):
            payload = json.dumps({"name": name, "delete": True})
            sends.append(
                client.publish(
                    f"nodes/{node}/stop",
                    payload,
                    qos=1,
                    **signed_publish_kwargs(f"nodes/{node}/stop", payload),
                )
            )
    await asyncio.gather(*sends, return_exceptions=True)


def _restartable(agent: str | None) -> tuple[Any, list[Any]]:
    """The supervisor, and the running agents a state reset starts again: `agent`, or all, less the system's own.

    Only those the supervisor holds, since it is what brings them back; a
    protected one is the system's -- main, the monitor, the installer, the
    catalogue -- and keeps running with its copies cleared instead.
    """
    supervisor = getattr(runtime.registry, "_supervisor_ref", None) if runtime.registry else None
    if supervisor is None or runtime.registry is None:
        return None, []
    return supervisor, [
        actor
        for actor in runtime.registry.all_actors()
        if (agent is None or actor.name == agent)
        and not getattr(actor, "protected", False)
        and actor.name in supervisor._specs
    ]


def _places(main_actor: Any, name: str) -> list[Address]:
    """Every place an agent called ``name`` is kept or runs, running or not.

    Main's own process, or main as its spawn registry places it; then every
    node anything says runs it (see `_agents_on_nodes`).
    """
    places: list[Address] = []
    registry = main_actor._get_spawn_registry() if main_actor is not None else {}
    entry = registry.get(name)
    here = runtime.registry is not None and runtime.registry.find_by_name(name) is not None
    if here or (entry is not None and not (entry.get("node") or "").strip()):
        places.append(Address(name, MAIN_NODE))
    places += [
        Address(name, node)
        for node, names in sorted(_agents_on_nodes(main_actor).items())
        if name in names
    ]
    return places


def _reset_target(wanted: str) -> tuple[str, Response | None]:
    """The name a reset of ``wanted`` (a name or ``name@node``) acts on, or the answer refusing it.

    What is reset is kept by the agent's name: main's chat log, metrics and
    spawn entry, and the copies on every node that runs the name. So a reset is
    refused when the name is in more than one place -- for a bare name, with
    the addresses; for an address, because the others would be reset with it --
    and an address is refused when the name is not on its node.
    """
    main_actor = find_main_actor(runtime.registry)
    if SEPARATOR in wanted and _places(main_actor, wanted.strip()):
        # A whole name, taken before the separator was refused in one.
        return wanted.strip(), None
    address = parse(wanted)
    places = _places(main_actor, address.name)
    if address.node and address not in places:
        return address.name, web.json_response(
            {"error": f"No agent '{address.name}' is on node '{address.node}'."}, status=404
        )
    if len(places) > 1:
        if not address.node:
            error = Resolution(address.name, tuple(places)).refusal()
        else:
            error = (
                f"Agents called '{address.name}' are on {' and '.join(p.node for p in places)}, "
                "and what a reset clears is kept by name, so one of them cannot be reset alone; "
                "nothing was done."
            )
        return address.name, web.json_response({"error": error}, status=409)
    return address.name, None


async def reset_handler(request: web.Request) -> Response:
    """POST /api/reset  —  clear stored state and ws.broadcast a reset event.

    Body (JSON):
      scope   : "chat" | "state" | "metrics" | "spawns" | "all"  (required)
      agent   : str  (optional — limit to one agent, by name or ``name@node``)
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    scope = body.get("scope", "")
    agent = body.get("agent") or None
    if agent:
        agent, refusal = _reset_target(str(agent))
        if refusal is not None:
            return refusal

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
                # Forgotten, not released: a factory reset removes these agents
                # for good, and a retired entry would outlive them.
                for actor in stoppable:
                    supervisor.drop_supervised(actor.name)
            await asyncio.gather(
                *[forget_actor(actor) for actor in stoppable], return_exceptions=True
            )
            await asyncio.gather(
                *[runtime.registry.unregister(a.actor_id) for a in stoppable if runtime.registry],
                return_exceptions=True,
            )

            # Delete the agents running on nodes, not in this registry. The
            # nodes themselves stay: a reset clears what was built on the
            # install, and a node is the machine it runs on.
            main_actor = find_main_actor(runtime.registry)
            await _delete_on_nodes(_agents_on_nodes(main_actor))

            # Purge retained MQTT for EVERY non-protected agent, tombstone each so a
            # late/in-flight frame can't re-admit it once _hard_resetting clears, and
            # drop it from the dashboard now.
            #
            # Suppressed on main's side for the duration: each purge withdraws a
            # manifest, and main reads a withdrawal as that agent removing
            # itself — which would record a deletion note per wiped agent for a
            # removal this reset is already performing. A tombstone arriving
            # after the window finds an emptied spawn registry and stops there.
            with _withdrawals_suppressed(main_actor):
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
                reset_actor_cost(actor)
                forget_conversation(actor)
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
            # Everything, whatever agent was named: the agents were all stopped
            # above, and records kept for some would be a half-wiped install.
            _reset.reset_all(None)
            # Clear the in-memory lifetime cost ledger + global accumulator too, or
            # the headline total re-pins to the old high-water once _hard_resetting
            # clears and heartbeats resume (mirrors /api/cost/reset).
            cost.lifetime_cost.clear()
            try:
                llm_cost.reset_global_cost()
            except Exception as exc:
                logger.debug("[reset] reset_global_cost skipped: %s", exc)
            runtime.state["agents"].clear()
            # Each node is kept, running nothing now; its next heartbeat says so too.
            for listed in runtime.state["nodes"].values():
                listed["agents"] = []
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
        return web.json_response({"status": "ok", "scope": "all", "agent": None, "nodes": {}})
    if scope == "chat":
        _reset.reset_chat(agent)
    elif scope == "state":
        # An agent forgets its state only once it starts again: what it read at
        # start is in memory, and its next persist writes it back. So each is
        # restarted with the file gone -- except the system's own agents, main
        # among them, which this request runs through.
        supervisor, restarting = _restartable(agent)
        for actor in restarting:
            await supervisor.restart_afresh(
                actor.name, lambda name=actor.name: _reset.reset_agent_state(name)
            )
        if agent:
            _reset.reset_agent_state(agent)
        else:
            _reset._reset_all_pickles()
    elif scope == "metrics":
        _reset.reset_metrics(agent)
        live_actors = list(runtime.registry.all_actors()) if runtime.registry is not None else []
        # The headline total is max(live + historical, lifetime ledger).
        # reset_metrics cleared the kv ledger, but the in-memory cost.lifetime_cost
        # high-water survives in THIS process and pins the headline to its old
        # value — so the total only "drops" by the live component and never
        # zeroes. Clear it here, mirroring /api/cost/reset.
        if agent:
            # The dashboard's agents as well as this process's: an agent on a
            # node banks its cost here too, under an id no local actor has.
            aid = next(
                (getattr(a, "actor_id", None) for a in live_actors if a.name == agent), None
            ) or next(
                (k for k, v in runtime.state["agents"].items() if v.get("name") == agent), None
            )
            if aid:
                cost.forget_lifetime_cost(aid)
        else:
            cost.lifetime_cost.clear()
            try:
                llm_cost.reset_global_cost()
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

    nodes: dict[str, str] = {}
    if scope in LIVE_SCOPES:
        # The disk is cleared. The agents running in this process hold copies of
        # their own and write them back on their next turn, persist or heartbeat
        # unless those go too, and an agent on a node keeps its copy there.
        for actor in list(runtime.registry.all_actors()) if runtime.registry is not None else []:
            if agent is None or actor.name == agent:
                reset_live(actor, scope)
        main_actor = find_main_actor(runtime.registry)
        if main_actor is not None:
            nodes = await node_reset.reset_on_nodes(
                main_actor, scope, agent, _agents_on_nodes(main_actor)
            )

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
    return web.json_response({"status": "ok", "scope": scope, "agent": agent, "nodes": nodes})
