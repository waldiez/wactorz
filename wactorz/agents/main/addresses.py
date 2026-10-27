"""Where main knows an agent to be, and so which agents a name or an address finds.

Every way a person or a program names an agent -- a chat mention, a
delegation, a generated agent's ``send_to``, a CLI or REST command -- asks
here, so a name that more than one agent answers to is noticed in one place
and never settled by taking the first. `wactorz.core.addresses` says what a
name finds; this module says what main knows of.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from ...core.addresses import MAIN_NODE, Address, Resolution, node_of, parse, resolve
from .voices import placed_node

if TYPE_CHECKING:
    from .hosts import AddressHost

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
        placed = placed_node(host, name)
        if placed and placed not in host.nodes.known:
            found.append(Address(name, placed))
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


def task_refusal(host: AddressHost, address: Address) -> str | None:
    """Why a task for the agent at ``address`` cannot be sent yet, or None if it can.

    A task for an agent on a node goes on ``agents/by-name/<name>/task``, which
    every node subscribes to, and each node runs its own agent of that name.
    While more than one node runs the name, a task meant for one of them would
    run on all of them, so none is sent. An agent in main's own process is
    asked in-process and is never affected.
    """
    if address.node in ("", MAIN_NODE):
        return None
    nodes = sorted({a.node for a in known_addresses(host, address.name)} - {MAIN_NODE})
    if len(nodes) <= 1:
        return None
    return (
        f"Agents called '{address.name}' run on nodes {' and '.join(nodes)}, and a task for "
        f"one of them still reaches every node that runs the name, so nothing was sent to "
        f"{address}."
    )


def registered_name(host: AddressHost, wanted: str) -> tuple[str, str | None]:
    """The name of the agent ``wanted`` (a name or ``name@node``) names in the spawn registry, or why none.

    For what acts on what the spawn registry keeps, one entry per name, such
    as restarting, stopping or moving an agent: a bare name names that entry.
    An address has to agree with where the registry places it, so a command
    typed for an agent on one node never acts on the agent of that name
    somewhere else.
    """
    address = parse(wanted)
    if not address.node:
        return address.name, None
    entry = host._get_spawn_registry().get(address.name)
    if entry is None:
        if address.node == MAIN_NODE:
            return address.name, None
        return address.name, f"No agent '{address.name}' is kept on node '{address.node}'."
    placed = node_of(str(entry.get("node") or MAIN_NODE))
    if placed != address.node:
        return address.name, f"'{address.name}' is on '{placed}', not on '{address.node}'."
    return address.name, None
