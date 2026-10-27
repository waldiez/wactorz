"""Where main knows an agent to be, and so which agents a name or an address finds.

Every way a person or a program names an agent -- a chat mention, a
delegation, a generated agent's ``send_to``, a CLI or REST command -- asks
here, so a name that more than one agent answers to is noticed in one place
and never settled by taking the first. `wactorz.core.addresses` says what a
name finds; this module says what main knows of.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, NamedTuple

from ...core.addresses import MAIN_NODE, Address, Resolution, parse, resolve
from ...core.topics import task_by_name_topic, task_topic
from .spawn_registry import address_of, placed_on
from .voices import placed_nodes

if TYPE_CHECKING:
    from .hosts import AddressHost, SpawnRegistryHost

#: How long after main starts a node it has not yet heard from is taken to run
#: what the spawn registry places on it. Heartbeats arrive far more often.
UNHEARD_GRACE_SECONDS = 30


def known_addresses(
    host: AddressHost, name: str, *, heard_within: float | None = None
) -> list[Address]:
    """Every place main knows an agent called ``name`` to run.

    Main's own process first. Then each node whose latest heartbeat lists the
    agent and that may speak for it -- with ``heard_within``, only one heard
    from that recently. Just after main starts no node has reported yet,
    though the nodes kept running, so for that long the spawn registry answers
    for a node main has not heard from.
    """
    found: list[Address] = []
    if host._registry is not None and host._registry.find_by_name(name):
        found.append(Address(name, MAIN_NODE))
    now = time.time()
    for node in host.nodes.nodes_for(name):
        seen = host.nodes.known.get(node, {}).get("last_seen", 0)
        if heard_within is None or now - seen < heard_within:
            found.append(Address(name, node))
    if now - host.metrics.start_time < UNHEARD_GRACE_SECONDS:
        found.extend(
            Address(name, placed)
            for placed in placed_nodes(host, name)
            if placed not in host.nodes.known
        )
    return found


def resolve_address(
    host: AddressHost, wanted: str, *, heard_within: float | None = None
) -> Resolution:
    """Which agents ``wanted``, a bare name or ``name@node``, finds among those main knows of."""
    names = dict.fromkeys((wanted.strip(), parse(wanted).name))
    known = [
        address
        for name in names
        if name
        for address in known_addresses(host, name, heard_within=heard_within)
    ]
    return resolve(wanted, known)


def task_topic_for(host: AddressHost, address: Address) -> str:
    """The topic a task for the agent at ``address``, on a node, goes on.

    Its node's own, ``agents/at/<node>/<name>/task``, when the node's manifest
    says it takes tasks there. Otherwise by name, as a node from before that
    takes them -- and as one whose manifest has not arrived yet is taken to,
    rather than holding the task until it does.
    """
    if host.nodes.takes_tasks_by_node(address.node):
        return task_topic(address.node, address.name)
    return task_by_name_topic(address.name)


def task_refusal(host: AddressHost, address: Address) -> str | None:
    """Why a task for the agent at ``address`` cannot be sent, or None if it can.

    A task that goes by name (see `task_topic_for`) reaches every node that
    runs the name, each of which runs its own agent of that name -- a node on
    a later release hears by name as well. So one is sent only while no other
    node runs the name. An agent in main's own process is asked in-process, and
    one whose node takes tasks on its own topic is reached alone.
    """
    if address.node in ("", MAIN_NODE) or host.nodes.takes_tasks_by_node(address.node):
        return None
    nodes = sorted({a.node for a in known_addresses(host, address.name)} - {MAIN_NODE})
    if len(nodes) <= 1:
        return None
    return (
        f"Agents called '{address.name}' run on nodes {' and '.join(nodes)}, and node "
        f"'{address.node}' takes a task only by name, which every one of them would run, "
        f"so nothing was sent to {address}. Deploy '{address.node}' again to address it alone."
    )


class Registered(NamedTuple):
    """The agent a command names in the spawn registry.

    ``node`` is where the registry places it, "" for main; ``entry`` is its
    registry entry, None for an agent main runs that the registry does not
    keep, such as a built-in one.
    """

    name: str
    node: str = ""
    entry: dict[str, Any] | None = None


def registered_agent(host: SpawnRegistryHost, wanted: str) -> tuple[Registered, str | None]:
    """The agent ``wanted`` (a name or ``name@node``) names in the spawn registry, or why none.

    For what acts on one agent main keeps, such as restarting, stopping,
    deleting or moving it. A bare name names its only entry, and a name kept
    in more than one place names none: the refusal says the addresses. An
    address has to agree with where the registry places it, so a command
    typed for an agent on one node never acts on the agent of that name
    somewhere else.
    """
    address = parse(wanted)
    registry = host._get_spawn_registry()
    entries = registry.named(address.name)
    if not address.node:
        if len(entries) > 1:
            found = Resolution(address.name, tuple(address_of(e) for e in entries))
            return Registered(address.name), found.refusal()
        entry = entries[0] if entries else None
    else:
        entry = registry.at(address.name, address.node)
        if entry is None and (entries or address.node != MAIN_NODE):
            if not entries:
                return Registered(address.name), (
                    f"No agent '{address.name}' is kept on node '{address.node}'."
                )
            places = " and ".join(f"'{address_of(e).node}'" for e in entries)
            return Registered(address.name), (
                f"'{address.name}' is on {places}, not on '{address.node}'."
            )
    if entry is None:
        return Registered(address.name), None
    return Registered(address.name, placed_on(entry), entry), None
