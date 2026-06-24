"""Composable behaviour mixins for the agent classes.

Each mixin bundles one concern (spawning, memory, …) and is mixed into an
Actor-derived class. They are re-exported here so callers import from the
package — ``from .mixins import SpawnMixin`` — rather than depending on which
module a mixin currently lives in. When a concern is split or renamed, only this
file changes; no call site moves.
"""

from .spawning import SpawnMixin, _SpawnPlaceholder
from .memory import MemoryMixin
from .routing import RoutingMixin
from .planning import PlanningMixin

__all__ = [
    "SpawnMixin",
    "MemoryMixin",
    "RoutingMixin",
    "PlanningMixin",
    "_SpawnPlaceholder",
]