# SPDX-License-Identifier: Apache-2.0
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
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

from aiohttp import web
from aiohttp.web import Response

from .. import reset as _reset
from ..agents.llm import cost as llm_cost
from ..agents.lookup import find_main_actor
from ..agents.main.spawn_registry import SpawnRegistry, placed_on
from ..core.addresses import MAIN_NODE, SEPARATOR, Address, Resolution, node_of, parse
from ..core.agent_reset import (
    LIVE_SCOPES,
    forget_conversation,
    forget_legacy_state,
    reset_actor_cost,
    reset_live,
)
from ..core.node_signing import signed_publish_kwargs
from . import cost, db_writer, events, lifecycle, node_reset, runtime, ws

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
    registry = main_actor._get_spawn_registry() if main_actor is not None else SpawnRegistry()
    for config in registry:
        node = placed_on(config)
        if node:
            on.setdefault(node, set()).add(config["name"])
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
    registry = main_actor._get_spawn_registry() if main_actor is not None else SpawnRegistry()
    here = runtime.registry is not None and runtime.registry.find_by_name(name) is not None
    if here or registry.at(name, MAIN_NODE) is not None:
        places.append(Address(name, MAIN_NODE))
    places += [
        Address(name, node)
        for node, names in sorted(_agents_on_nodes(main_actor).items())
        if name in names
    ]
    return places


@dataclass(frozen=True)
class _Target:
    """Whom a reset is for: everyone, an agent by its name, or one of two by its address.

    A name kept in one place is reset by name, which reaches everything of it,
    its turns from before it had an id included. ``address`` is set where the
    name is in more than one place: a name is unique on a host, so what a host
    keeps by name is that host's agent's alone, and of what main keeps for
    every host, only what ``actor_id`` or the node says is this agent's goes.
    """

    name: str | None = None
    address: Address | None = None
    actor_id: str | None = None

    @property
    def said(self) -> str | None:
        """The agent, as the answer names it."""
        return str(self.address) if self.address else self.name

    @property
    def on_main(self) -> bool:
        """Whether the reset reaches what main keeps for its own agents."""
        return self.address is None or self.address.node == MAIN_NODE

    @property
    def history_node(self) -> str | None:
        """The node whose metrics samples are this agent's; None for any, "" for main."""
        if self.address is None:
            return None
        return "" if self.on_main else self.address.node

    def covers(self, actor: Any) -> bool:
        """Whether ``actor``, running in main's process, is one this reset reaches."""
        return self.on_main and (self.name is None or actor.name == self.name)

    def nodes_of(self, on: dict[str, set[str]]) -> dict[str, set[str]]:
        """Of the agents each node runs, those this reset reaches."""
        if self.address is None:
            return on
        return {} if self.on_main else {self.address.node: {self.address.name}}


def _id_at(main_actor: Any, address: Address) -> str | None:
    """The id of the agent at ``address``: main's record of it, or what it last said it was."""
    registry = main_actor._get_spawn_registry() if main_actor is not None else SpawnRegistry()
    entry = registry.at(address.name, address.node)
    if entry is not None:
        return entry["actor_id"]
    for actor_id, agent in runtime.state["agents"].items():
        place = node_of(str(agent.get("node") or MAIN_NODE))
        if agent.get("name") == address.name and place == address.node:
            return actor_id
    if address.node == MAIN_NODE and runtime.registry is not None:
        actor = runtime.registry.find_by_name(address.name)
        return actor.actor_id if actor is not None else None
    return None


def _reset_target(wanted: str) -> tuple[_Target, Response | None]:
    """Whom a reset of ``wanted`` (a name or ``name@node``) is for, or the answer refusing it.

    A bare name in more than one place does not say which, and is refused with
    the addresses; an address is refused when the name is not on its node, or
    when the agent there has no id to tell it from the others by.
    """
    main_actor = find_main_actor(runtime.registry)
    if SEPARATOR in wanted and _places(main_actor, wanted.strip()):
        # A whole name, taken before the separator was refused in one.
        return _Target(wanted.strip()), None
    address = parse(wanted)
    places = _places(main_actor, address.name)
    if address.node and address not in places:
        return _Target(address.name), web.json_response(
            {"error": f"No agent '{address.name}' is on node '{address.node}'."}, status=404
        )
    if len(places) == 1 or not places:
        return _Target(address.name), None
    if not address.node:
        error = Resolution(address.name, tuple(places)).refusal()
        return _Target(address.name), web.json_response({"error": error}, status=409)
    actor_id = _id_at(main_actor, address)
    if actor_id is None:
        error = (
            f"Agents called '{address.name}' are on {' and '.join(p.node for p in places)}, "
            f"and nothing says which id the one on '{address.node}' has, so it cannot be told "
            "from the others; nothing was done."
        )
        return _Target(address.name), web.json_response({"error": error}, status=409)
    return _Target(address.name, address, actor_id), None


#: Every scope a reset takes. ``all`` is the factory reset; each other one clears one kind of record.
SCOPES = frozenset({"chat", "state", "metrics", "spawns", "logs", "all"})


async def reset_handler(request: web.Request) -> Response:
    """POST /api/reset  —  clear stored state and ws.broadcast a reset event.

    Body (JSON):
      scope   : "chat" | "state" | "metrics" | "spawns" | "logs" | "all"  (required)
      agent   : str  (optional — limit to one agent, by name or ``name@node``)
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    scope = body.get("scope", "")
    target = _Target()
    if body.get("agent"):
        target, refusal = _reset_target(str(body["agent"]))
        if refusal is not None:
            return refusal

    if scope not in SCOPES:
        return web.json_response({"error": f"scope must be one of {sorted(SCOPES)}"}, status=400)
    if scope == "all":
        return await _factory_reset()
    return await _scoped_reset(scope, target)


# ── The factory reset ──────────────────────────────────────────────────────


async def _factory_reset() -> Response:
    """Stop and forget every agent a fresh boot would not recreate, wipe the disk, and say so."""
    # Block incoming heartbeats while the wipe runs. Cleared however the wipe
    # ends: left set, every incoming heartbeat stays blocked, freezing the
    # dashboard until the process restarts.
    runtime.hard_resetting = True
    try:
        await _wipe_everything()
    finally:
        runtime.hard_resetting = False

    # The kept agents were never stopped — only forgotten, because the state
    # was cleared. The registry knows them all, in memory, now — so rebuild
    # from it and send ONE frame whose state already carries the survivors.
    # Both calls must follow the flag clearing: `update_agent` drops writes and
    # `snapshot` returns empty lists while a hard reset is in progress.
    restored = events.rebuild_from_registry(runtime.registry)
    if restored:
        logger.info("[reset] restored %d running agent(s) to the dashboard", restored)
    await cost.settled_totals()
    await ws.broadcast({"type": "reset", "scope": "all", "agent": None, "state": events.snapshot()})
    return web.json_response({"status": "ok", "scope": "all", "agent": None, "nodes": {}})


def _kept_actor(actor: Any) -> bool:
    """Whether a factory reset keeps ``actor``, by the protected flag the registry holds for it."""
    return survives_factory_reset(actor.name, getattr(actor, "protected", False))


async def _wipe_everything() -> None:
    """The factory reset's work, while heartbeats are blocked."""
    all_actors = list(runtime.registry.all_actors()) if runtime.registry is not None else []
    stoppable = [a for a in all_actors if not _kept_actor(a)]
    kept_actors = [a for a in all_actors if _kept_actor(a)]
    agent_ids = list({a.actor_id for a in stoppable} | set(_dashboard_ids_to_drop(kept_actors)))

    await _stop_for_good(stoppable)

    # Delete the agents running on nodes, not in this registry. The nodes
    # themselves stay: a reset clears what was built on the install, and a
    # node is the machine it runs on.
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
    # The spawn registry lives in the kv alone, so no actor holds a copy of it.
    await lifecycle.purge_spawn_reconcile(None)
    # Reset in-memory metrics + history on the KEPT actors (protected +
    # fresh-boot) so they too return to factory state, not just stay alive.
    for actor in kept_actors:
        _to_factory_state(actor)
    # Everything, whatever agent was named: the agents were all stopped
    # above, and records kept for some would be a half-wiped install.
    # Chat turns still queued for the log are from before the reset: written
    # after the wipe, they would bring those turns back.
    await db_writer.writer.settled()
    # On a thread: the wipe is the disk's. What runs meanwhile cannot bring
    # the old state back: the agents that went are stopped, and those kept
    # had their memory cleared above.
    await asyncio.to_thread(_reset.reset_all, None)
    # Clear the in-memory lifetime cost ledger + global accumulator too, or
    # the headline total re-pins to the old high-water once _hard_resetting
    # clears and heartbeats resume (mirrors /api/cost/reset).
    cost.reset_lifetime()
    await _reset_global_cost()
    runtime.state["agents"].clear()
    # Each node is kept, running nothing now; its next heartbeat says so too.
    for listed in runtime.state["nodes"].values():
        listed["agents"] = []
    runtime.state["alerts"].clear()
    runtime.state["log_feed"].clear()


def _dashboard_ids_to_drop(kept_actors: list[Any]) -> list[str]:
    """The dashboard's agents a factory reset tears down.

    Every one it does not keep, which covers an agent present only via MQTT,
    or a remote agent absent from this registry. The registry is the
    AUTHORITATIVE source of the protected flag — the dashboard entry's
    "protected" is only set when a heartbeat happened to carry it, so trusting
    it alone wrongly tears down system agents (main / monitor / installer /
    catalog / the HA family) and they flicker back.
    """
    kept_ids = {a.actor_id for a in kept_actors}
    kept_names = {a.name for a in kept_actors}
    return [
        aid
        for aid, ag in runtime.state["agents"].items()
        if not survives_factory_reset(ag.get("name", ""), ag.get("protected", False))
        and aid not in kept_ids
        and ag.get("name") not in kept_names
    ]


async def _stop_for_good(stoppable: list[Any]) -> None:
    """Release, stop and unregister the agents here that a factory reset removes."""
    # Released first, so the Supervisor doesn't race to restart them.
    supervisor = (
        getattr(runtime.registry, "_supervisor_ref", None) if runtime.registry is not None else None
    )
    if supervisor is not None:
        # Forgotten, not released: a factory reset removes these agents
        # for good, and a retired entry would outlive them.
        for actor in stoppable:
            supervisor.drop_supervised(actor.name)
    await asyncio.gather(*[forget_actor(actor) for actor in stoppable], return_exceptions=True)
    await asyncio.gather(
        *[runtime.registry.unregister(a.actor_id) for a in stoppable if runtime.registry],
        return_exceptions=True,
    )


def _to_factory_state(actor: Any) -> None:
    """Return a kept agent to how a fresh boot starts it: no counters, cost, conversation or memory."""
    actor.metrics.messages_processed = 0
    actor.metrics.errors = 0
    actor.metrics.tasks_completed = 0
    actor.metrics.tasks_failed = 0
    reset_actor_cost(actor)
    forget_conversation(actor)
    if hasattr(actor, "_user_facts"):
        actor._user_facts = {}
    if hasattr(actor, "_pipeline_rules"):
        actor._pipeline_rules = []
    # The attributes above are the live copies; this is the one behind them.
    # Without it `recall` keeps answering from the pickle this wipe deleted,
    # and every reset looks like it failed.
    forget_legacy_state(actor)


async def _reset_global_cost() -> None:
    """Zero the LLM spend accumulated across the process; a store that cannot be written is skipped."""
    try:
        await asyncio.to_thread(llm_cost.reset_global_cost)
    except Exception as exc:
        logger.debug("[reset] reset_global_cost skipped: %s", exc)


# ── One kind of record ─────────────────────────────────────────────────────


async def _scoped_reset(scope: str, target: _Target) -> Response:
    """Clear one kind of record, for everyone or for the agent ``target`` names, and say so."""
    if scope in LIVE_SCOPES:
        # Before the wipe, which runs on a thread: an agent here holds a copy of
        # its own and writes it back on its next turn, persist or heartbeat, so it
        # forgets first, and whatever it writes while the disk is cleared is
        # already the cleared state.
        for actor in list(runtime.registry.all_actors()) if runtime.registry is not None else []:
            if target.covers(actor):
                reset_live(actor, scope)
    await _CLEARS[scope](target)

    nodes: dict[str, str] = {}
    if scope in LIVE_SCOPES:
        # The disk is cleared, and the agents here forgot before it was. An agent
        # on a node keeps its copy there, so the node is told too.
        main_actor = find_main_actor(runtime.registry)
        if main_actor is not None:
            nodes = await node_reset.reset_on_nodes(
                main_actor, scope, target.name, target.nodes_of(_agents_on_nodes(main_actor))
            )
    if scope == "metrics":
        _clear_dashboard_metrics(target)

    await cost.settled_totals()
    await ws.broadcast(
        {
            "type": "reset",
            "scope": scope,
            "agent": target.said,
            "agent_id": target.actor_id,
            "state": events.snapshot(),
        }
    )
    return web.json_response({"status": "ok", "scope": scope, "agent": target.said, "nodes": nodes})


async def _clear_chat(target: _Target) -> None:
    # As for everything: queued turns land before the wipe, not after it.
    await db_writer.writer.settled()
    await asyncio.to_thread(
        _reset.reset_chat, target.name, actor_id=target.actor_id, on_main=target.on_main
    )


async def _clear_state(target: _Target) -> None:
    """Restart each agent here with its state file gone, and remove the files.

    An agent forgets its state only once it starts again: what it read at
    start is in memory, and its next persist writes it back. The system's own
    agents, main among them, which this request runs through, are not
    restarted.
    """
    agent = target.name
    supervisor, restarting = _restartable(agent)
    for actor in restarting if target.on_main else ():
        await supervisor.restart_afresh(
            actor.name, lambda name=actor.name: _reset.reset_agent_state(name)
        )
    if not agent:
        await asyncio.to_thread(_reset._reset_all_pickles)
    elif target.on_main:
        await asyncio.to_thread(_reset.reset_agent_state, agent)


async def _clear_metrics(target: _Target) -> None:
    """Remove the stored metrics, and the in-memory cost that would pin the headline total."""
    agent = target.name
    await asyncio.to_thread(
        _reset.reset_metrics, agent, node=target.history_node, on_main=target.on_main
    )
    # The headline total is max(live + historical, lifetime ledger).
    # reset_metrics cleared the kv ledger, but the in-memory cost.lifetime_cost
    # high-water survives in THIS process and pins the headline to its old
    # value — so the total only "drops" by the live component and never
    # zeroes. Clear it here, mirroring /api/cost/reset.
    if not agent:
        cost.reset_lifetime()
        await _reset_global_cost()
        return
    aid = _cost_id(target)
    if aid:
        cost.forget_lifetime_cost(aid)


def _cost_id(target: _Target) -> str | None:
    """The id the cost of the agent ``target`` names is banked under.

    The dashboard's agents as well as this process's: an agent on a node banks
    its cost here too, under an id no local actor has.
    """
    if target.actor_id:
        return target.actor_id
    live_actors = list(runtime.registry.all_actors()) if runtime.registry is not None else []
    return next(
        (getattr(a, "actor_id", None) for a in live_actors if a.name == target.name), None
    ) or next((k for k, v in runtime.state["agents"].items() if v.get("name") == target.name), None)


async def _clear_spawns(target: _Target) -> None:
    # Clear live state + retained desired_state FIRST (it needs the registry
    # to learn the affected nodes), then wipe the kv on disk. Cleared on disk
    # alone, the retained desired_state would stay, and a runner reconnecting
    # would reconcile the agents straight back.
    await lifecycle.purge_spawn_reconcile(target.name, target.actor_id)
    await asyncio.to_thread(_reset.reset_spawns, target.name, actor_id=target.actor_id)


async def _clear_logs(_target: _Target) -> None:
    await asyncio.to_thread(_reset.reset_logs)
    # The activity feed mirrors the log files just truncated, so drop the
    # in-memory entries too — otherwise the UI keeps showing stale lines.
    runtime.state["log_feed"].clear()


#: What each scope but ``all`` clears, given whom the reset is for.
_CLEARS: dict[str, Callable[[_Target], Awaitable[None]]] = {
    "chat": _clear_chat,
    "state": _clear_state,
    "metrics": _clear_metrics,
    "spawns": _clear_spawns,
    "logs": _clear_logs,
}


def _clear_dashboard_metrics(target: _Target) -> None:
    """Drop the dashboard's cost and message counts for the agents a metrics reset covers.

    Only a metrics reset: clearing chat history must not zero cost or message
    counters, or wipe the alerts and the activity feed.
    """
    agents = runtime.state["agents"]
    if target.name:
        aid = target.actor_id or next(
            (k for k, v in agents.items() if v.get("name") == target.name), None
        )
        affected = [aid] if aid and aid in agents else []
    else:
        affected = list(agents)
        runtime.state["alerts"].clear()
        runtime.state["log_feed"].clear()
    for aid in affected:
        agents[aid].pop("cost_usd", None)
        agents[aid].pop("messages_processed", None)
