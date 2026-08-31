"""Composable behaviour mixins for the agent classes.

Each mixin bundles one concern (spawning, memory, …) and is mixed into an
Actor-derived class. They are re-exported here so callers import from the
package — ``from .mixins import SpawnMixin`` — rather than depending on which
module a mixin currently lives in. When a concern is split or renamed, only this
file changes; no call site moves.
"""

from .spawning import SpawnMixin, SpawnPlaceholder

__all__ = [
    "SpawnMixin",
    "SpawnPlaceholder",
]
