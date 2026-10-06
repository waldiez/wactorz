"""Whether an agent's state can travel with it when it migrates.

A migration ships the state as JSON over MQTT, and the receiving side keeps it
as JSON. A value that does not serialise cannot go, and a snapshot too large
for the broker or for a small node to parse cannot either. Main and the node
runner both ask the questions here, so the two legs of a migration refuse on
the same terms and in the same words.
"""

from __future__ import annotations

import json
from typing import Any

#: The word a `/migrate` command takes to move an agent whose state holds values
#: that cannot travel, leaving those behind.
FORCE_FLAG = "--force"


def json_safe(values: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The part of ``values`` that can travel over MQTT, and the keys that cannot."""
    safe: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in values.items():
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            dropped.append(key)
        else:
            safe[key] = value
    return safe, dropped


def encoded_size(state: dict[str, Any]) -> int:
    """How many bytes ``state`` takes on the wire, as JSON."""
    return len(json.dumps(state).encode("utf-8"))


def why_it_cannot_travel(
    state: dict[str, Any],
    dropped: list[str],
    *,
    force: bool,
    max_bytes: int | None,
) -> str | None:
    """The reason a snapshot must not be shipped, or None when it may.

    Keys that cannot be encoded are a refusal unless ``force`` is set: the agent
    would arrive without them, and nothing on the other side could tell. A
    snapshot over ``max_bytes`` is refused whatever ``force`` says, since
    forcing it would not make it arrive. ``max_bytes`` of None or 0 sets no
    limit.
    """
    if dropped and not force:
        return (
            f"its state holds {', '.join(sorted(dropped))}, which cannot travel as JSON "
            f"and would be lost. Migrate with {FORCE_FLAG} to move it without them."
        )
    if max_bytes:
        size = encoded_size(state)
        if size > max_bytes:
            return (
                f"its state is {size} bytes, over the {max_bytes}-byte limit for a "
                f"migration (WACTORZ_MIGRATION_MAX_STATE_BYTES)."
            )
    return None
