"""A spawn of a name that already runs on another host: a move, a second agent, or a question.

A name is unique on one host, not across them. So a spawn onto one node, of a
name that runs or is kept on another, may mean either of two things, and the
spawn says which: ``replace`` moves the agent there, keeping its id, and
``alongside`` adds a second agent of the name there, under an id of its own.
One that says neither is not guessed at: nothing is spawned, and whoever asked
is told both.

Two agents of a name on one host would answer to one address, so a spawn that
adds one there, or moves one there, is refused; renaming either on the way is
not offered, because a rename breaks every address that names it. A node that
gives an agent the id derived from its name, rather than the one its spawn
names, would give a second agent of the name the first one's id, so it is
not given one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...core.addresses import MAIN_NODE, Address, Resolution, node_of
from ..mixins import KeptHere, NotSpawned
from .spawn_registry import address_of, placed_on

if TYPE_CHECKING:
    from .hosts import SpawnHost

logger = logging.getLogger(__name__)


def target_of(config: dict[str, Any]) -> str:
    """The host a spawn of ``config`` goes to: its node, or `MAIN_NODE`."""
    return placed_on(config) or MAIN_NODE


def places_of(host: SpawnHost, name: str) -> list[Address]:
    """Every place an agent called ``name`` runs or is kept: main's own, the nodes', the registry's."""
    found = list(host.resolve_address(name).matches)
    found += [address_of(entry) for entry in host._get_spawn_registry().named(name)]
    return list(dict.fromkeys(Address(a.name, node_of(a.node)) for a in found if a.name == name))


@dataclass(frozen=True)
class Settled:
    """What to do with a spawn of a name that runs elsewhere.

    ``refused`` is the result to report instead of spawning; ``moved_from`` is
    the place an agent was stopped so that this spawn can start it again
    somewhere else, to be put back if the spawn does not happen.
    """

    refused: NotSpawned | KeptHere | None = None
    moved_from: Address | None = None


def question(name: str, target: str, elsewhere: list[Address]) -> KeptHere:
    """What a spawn of ``name`` onto ``target`` says when it runs ``elsewhere`` and the spawn said neither."""
    return KeptHere(name, target, tuple(str(a) for a in elsewhere))


def _alongside_refusal(host: SpawnHost, name: str, target: str) -> str | None:
    """Why a second agent of ``name`` cannot go to ``target``, or None when it can."""
    if target == MAIN_NODE or host._node_takes_actor_ids(target) is True:
        return None
    return (
        f"node '{target}' gives an agent the id derived from its name, which the other "
        f"agent called '{name}' may already have, so it does not take a second one. "
        f"Redeploy it with `/deploy {target}` first"
    )


def _replace_refusal(name: str, target: str, elsewhere: list[Address]) -> str | None:
    """Why ``replace`` cannot say which agent of ``name`` moves to ``target``, or None."""
    if len(elsewhere) <= 1:
        return None
    found = Resolution(name, tuple(elsewhere))
    return (
        f"agents called '{name}' run on more than one host ({found.choices()}), so "
        f"\"replace\" does not say which one moves to '{target}'. Move one with "
        f"`/migrate <address> {target}`"
    )


async def settle(host: SpawnHost, config: dict[str, Any]) -> Settled:
    """Decide a spawn of ``config`` against where its name already is, stopping what a move moves.

    A spawn onto the host the name is already on goes ahead: it updates, or
    replaces, that agent, as it always has. One onto a host the name is not
    on, while it is elsewhere, is decided by ``replace`` and ``alongside``.
    """
    name = str(config.get("name") or "")
    target = target_of(config)
    places = places_of(host, name)
    here = Address(name, target) in places
    elsewhere = [a for a in places if a.node != target]
    if not elsewhere:
        return Settled()
    if config.get("alongside"):
        if here:
            return Settled(NotSpawned(name, f"{Address(name, target)} already runs"))
        refusal = _alongside_refusal(host, name, target)
        return Settled(NotSpawned(name, refusal) if refusal else None)
    if here:
        return Settled()
    if not config.get("replace"):
        return Settled(question(name, target, elsewhere))
    refusal = _replace_refusal(name, target, elsewhere)
    if refusal:
        return Settled(NotSpawned(name, refusal))
    source = elsewhere[0]
    await _stop_at(host, source)
    return Settled(moved_from=source)


async def _stop_at(host: SpawnHost, source: Address) -> None:
    """Stop the agent at ``source`` for a move, leaving its registry entry for the spawn to rewrite.

    The entry keeps its id, so the agent keeps it wherever it goes next.
    """
    logger.info("[%s] Moving %s: stopping it there", host.name, source)
    if source.node == MAIN_NODE:
        existing = host._registry.find_by_name(source.name) if host._registry else None
        if existing is not None:
            await host._stop_for_replace(existing, source.name)
        return
    await host._update_node_desired_state(source.node, remove_name=source.name)
    await host._mqtt_publish(f"nodes/{source.node}/stop", {"name": source.name}, qos=1)


async def put_back(host: SpawnHost, source: Address) -> None:
    """Undo `_stop_at` for a move whose spawn did not happen: the agent runs where it was.

    On a node, its desired state names it again, which the node reconciles
    from. On main, its entry is restored from as at a start.
    """
    entry = host._get_spawn_registry().at(source.name, source.node)
    if entry is None:
        return
    logger.warning("[%s] The move of %s did not happen: putting it back", host.name, source)
    if source.node == MAIN_NODE:
        await host._spawn_local_from_config(entry, register=False, from_registry=True)
        return
    await host._update_node_desired_state(source.node, entry)
