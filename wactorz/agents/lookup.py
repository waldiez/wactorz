"""Typed lookup for the agents that are known by a fixed registry name.

`ActorRegistry.find_by_name` returns a plain `Actor`, which is the right answer
for a registry that must not know its tenants. It is the wrong answer at the
call sites that immediately want a subclass's own API, and they resolved it
themselves — some with a `cast`, some by probing with `hasattr`.

Only `main` earns an entry here. The other well-known names — `installer`,
`monitor`, `home-assistant-agent` — are looked up to check they exist and then
used through the base `Actor` interface, so a typed accessor for them would
return something the caller never treats as more than an `Actor`.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.registry import ActorRegistry
    from .main import MainActor

#: The registry name the orchestrator is always registered under.
MAIN_ACTOR_NAME = "main"


def find_main_actor(registry: "ActorRegistry | None") -> "MainActor | None":
    """The orchestrator, or None if it is not registered.

    `isinstance` rather than a cast: a cast is a promise the caller cannot
    keep, since anything registered under this name satisfies it and the
    resulting AttributeError surfaces far from the cause. A wrong occupant
    returns None instead, which every caller already handles — they have to,
    because the orchestrator is genuinely absent in legacy MQTT mode.

    `registry` is optional because most callers hold one that may be None, and
    a helper that made each of them write the same guard would not have saved
    them anything.
    """
    from .main import MainActor  # local: the main package imports this module

    actor = registry.find_by_name(MAIN_ACTOR_NAME) if registry else None
    return actor if isinstance(actor, MainActor) else None
