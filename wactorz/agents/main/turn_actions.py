"""What a turn did to the system, and the one sentence that reports it.

A turn answers the user, and on the way it may spawn agents, delete them, or
fail to find ones it was asked to delete. That is worth saying plainly, because
none of it is visible in the answer itself: the model writes prose claiming it
built something, and only this line says whether anything was actually built.

Kept apart from the turn paths that produce it. Two of them do — the plain reply
and the streamed one — and each used to write the sentence itself, which is how
they came to disagree about whether an agent's name is quoted. One object here
means the next difference between those paths has to be a deliberate one.

Nothing in here reaches for an actor or a broker. It is handed what happened and
asked to describe it, which is why it can be read and tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..mixins import SpawnPlaceholder


@dataclass(frozen=True)
class TurnActions:
    """What a turn did besides answer, and how to say so.

    The three lists are what the model's blocks actually produced. Held together
    because the sentence describing them is one sentence, and it was written
    twice — once per turn path — which is how the two came to quote agent names
    differently.

    A spawn that is still installing packages is reported apart from one that is
    already running: the agent will appear later, and saying nothing about it
    reads as the spawn having failed.
    """

    spawned: tuple[Any, ...] = ()
    deleted: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    def summary(self, response: str) -> str:
        """One line naming what happened, or empty when nothing did.

        `response` is the model's raw output, read only to tell a replacement
        from a fresh spawn — the distinction lives in the block it wrote, not in
        anything the spawn returned.
        """
        parts: list[str] = []
        live = [a.name for a in self.spawned if not isinstance(a, SpawnPlaceholder)]
        installing = [a.name for a in self.spawned if isinstance(a, SpawnPlaceholder)]
        if live:
            replaced = '"replace": true' in response or '"replace":true' in response
            action = "Replaced" if replaced else "Spawned"
            parts.append(f"{action} {_named(live)} — will auto-restore on restart")
        if installing:
            parts.append(f"Installing packages for {_named(installing)} — will appear shortly")
        # Deletions are unquoted because both paths already wrote them that way;
        # only the spawn line disagreed, and quoting is the half that survives a
        # hyphenated name.
        if self.deleted:
            parts.append(f"Deleted {', '.join(self.deleted)}")
        if self.missing:
            parts.append(f"Could not delete {', '.join(self.missing)} — not currently registered")
        return " | ".join(parts)


def _named(names: Any) -> str:
    """Agent names as a reader sees them, quoted so a hyphenated one holds together."""
    return ", ".join(f"'{n}'" for n in names)
