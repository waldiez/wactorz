"""What main remembers about the agents it spawned, one entry per agent.

An entry is the config an agent was spawned from, with the node it runs on and
the id it runs under. Entries are kept by that id, because a name is unique
only on one host: two agents called ``counter``, one on main and one on a
node, are two entries. Reading the registry is therefore asking for an agent
in one of a few ways -- by id, by name on a node, or by a name that is meant
to find exactly one -- and never indexing it by name.

The registry is persisted under `SPAWN_REGISTRY_KEY`. A store written before
entries were kept by id holds them by name under `NAME_KEYED_REGISTRY_KEY`;
it is read once, when the new key has nothing, and left as it was, so a
release from before finds the registry as it stood at the upgrade.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from ...core.actor import derive_actor_id
from ...core.addresses import MAIN_NODE, Address, node_of
from ...core.identity import given_actor_id

#: Persistence key for the registry, entries kept by actor id.
#:
#: Public because it is the restart contract: the spawn service writes it and
#: startup, the dashboard, migration and `/agents restart` all read it.
SPAWN_REGISTRY_KEY = "_spawned_agents_by_id"

#: Where a store from before entries were kept by id holds them, by name.
NAME_KEYED_REGISTRY_KEY = "_spawned_agents"

#: Config keys that instruct the one spawn carrying them, rather than describing
#: what the agent is.
#:
#: `_initial_state` is a state snapshot handed over at migration time. The runner
#: treats it as ground truth and deletes the agent's state file to apply it, which
#: is right once — the source was authoritative at that moment — and wrong every
#: time after, because the agent has been living on the target since. Kept in a
#: standing record it is re-applied on every reconcile, so a node reboot would
#: roll the agent back to the moment it arrived.
#:
#: `alongside` asks for a second agent of a name that runs elsewhere; once
#: there is one, it says nothing about it.
TRANSIENT_CONFIG_KEYS = frozenset({"_initial_state", "alongside"})


def without_transient_keys(config: dict[str, Any]) -> dict[str, Any]:
    """`config` without the keys that must not outlive the spawn they were sent with.

    Applied where a config is *recorded* — the spawn registry and a node's
    desired state — never to the spawn message itself, which is the hand-off
    those keys exist for and is retain-cleared by the runner once consumed.
    """
    return {k: v for k, v in config.items() if k not in TRANSIENT_CONFIG_KEYS}


def with_actor_id(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """A registry entry with its id: the one it keeps, or for an entry that has
    none, the id its agent already runs under, derived from its name.
    """
    return {**config, "actor_id": given_actor_id(config.get("actor_id")) or derive_actor_id(name)}


def placed_on(entry: Mapping[str, Any]) -> str:
    """The node ``entry`` places its agent on, or "" for main."""
    node = node_of(str(entry.get("node") or ""))
    return "" if node in ("", MAIN_NODE) else node


def address_of(entry: Mapping[str, Any]) -> Address:
    """``entry``'s agent's full address: its name and its node, `MAIN_NODE` for main."""
    return Address(str(entry.get("name") or ""), placed_on(entry) or MAIN_NODE)


def _cleaned(name: str, config: Any) -> dict[str, Any] | None:
    """A stored entry as the registry hands it out, or None for one that is not an entry."""
    if not isinstance(config, dict) or not name:
        return None
    return with_actor_id(name, {**without_transient_keys(config), "name": name})


class SpawnRegistry:
    """The spawn registry's entries, kept by actor id, and the ways of finding one.

    A snapshot: changing an entry goes through the spawn service, which saves
    the whole registry again.

    Entries are cleaned on the way out as well as on the way in. An entry
    recorded before the arrival snapshot was recognised as a one-time
    instruction still carries one, and every reader hands it back to a runner
    that treats it as ground truth -- the desired state a node reconciles
    from, a recovery re-spawn. Cleaning on read heals those entries wherever
    they are read, and the next write stores the cleaned form.
    """

    def __init__(self, entries: Mapping[str, dict[str, Any]] | None = None) -> None:
        self._entries: dict[str, dict[str, Any]] = dict(entries or {})

    @classmethod
    def from_names(cls, by_name: Any) -> SpawnRegistry:
        """The registry a store from before entries were kept by id holds: name to config.

        Each entry gets the id it keeps, or its name's derived id, which is the
        one its agent already runs under.
        """
        entries: dict[str, dict[str, Any]] = {}
        if isinstance(by_name, Mapping):
            for name, config in by_name.items():
                entry = _cleaned(str(name), config)
                if entry is not None:
                    entries[entry["actor_id"]] = entry
        return cls(entries)

    @classmethod
    def from_ids(cls, by_id: Any) -> SpawnRegistry:
        """The registry as `SPAWN_REGISTRY_KEY` stores it: actor id to entry."""
        entries: dict[str, dict[str, Any]] = {}
        if isinstance(by_id, Mapping):
            for actor_id, config in by_id.items():
                if not isinstance(config, dict):
                    continue
                entry = _cleaned(str(config.get("name") or ""), {"actor_id": actor_id, **config})
                if entry is not None:
                    entries[entry["actor_id"]] = entry
        return cls(entries)

    @classmethod
    def stored(cls, by_id: Any, by_name: Any = None) -> SpawnRegistry:
        """The registry a store holds: under the id-keyed key, or else the name-keyed one."""
        if isinstance(by_id, Mapping):
            return cls.from_ids(by_id)
        return cls.from_names(by_name)

    def stored_form(self) -> dict[str, dict[str, Any]]:
        """What is persisted under `SPAWN_REGISTRY_KEY`."""
        return {actor_id: dict(entry) for actor_id, entry in self._entries.items()}

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Every entry, as a copy the caller may change."""
        return (dict(entry) for entry in self._entries.values())

    def names(self) -> set[str]:
        """Every name an entry has."""
        return {entry["name"] for entry in self._entries.values()}

    def by_id(self, actor_id: str | None) -> dict[str, Any] | None:
        """The entry of the agent ``actor_id``, or None."""
        entry = self._entries.get(actor_id or "")
        return dict(entry) if entry is not None else None

    def named(self, name: str) -> list[dict[str, Any]]:
        """Every entry called ``name``, main's first, then by node."""
        found = [dict(e) for e in self._entries.values() if e["name"] == name]
        return sorted(found, key=placed_on)

    def at(self, name: str, node: str) -> dict[str, Any] | None:
        """The entry for ``name`` on ``node`` ("", `MAIN_NODE` or "local" for main), or None.

        At most one: a name is unique on a host.
        """
        wanted = placed_on({"node": node})
        return next((e for e in self.named(name) if placed_on(e) == wanted), None)

    def only(self, name: str) -> dict[str, Any] | None:
        """The entry for ``name`` when it is the only one, or None for none or several."""
        found = self.named(name)
        return found[0] if len(found) == 1 else None

    def on_node(self, node: str) -> list[dict[str, Any]]:
        """Every entry placed on ``node`` ("" for main)."""
        wanted = placed_on({"node": node})
        return [dict(e) for e in self._entries.values() if placed_on(e) == wanted]

    def with_entry(self, entry: dict[str, Any]) -> SpawnRegistry:
        """This registry with ``entry`` kept under its id."""
        return SpawnRegistry({**self._entries, entry["actor_id"]: dict(entry)})

    def without(self, actor_id: str) -> SpawnRegistry:
        """This registry without the entry ``actor_id``."""
        return SpawnRegistry({k: v for k, v in self._entries.items() if k != actor_id})
