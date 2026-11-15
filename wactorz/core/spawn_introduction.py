# SPDX-License-Identifier: Apache-2.0
"""The short introduction shown when a new agent has started."""

import logging

from .actor import Actor, ActorState

logger = logging.getLogger(__name__)


def _short_line(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    line = " ".join(value.split())
    if len(line) > limit:
        return line[: limit - 1].rstrip() + "…"
    return line


def spawn_introduction(description: object, examples: object, *, help_text: str = "") -> str:
    """Say what an agent does and give up to two nonempty examples or a help pointer."""
    about = _short_line(description, 280)
    suggestions = (
        [line for example in examples if (line := _short_line(example, 120))][:2]
        if isinstance(examples, (list, tuple))
        else []
    )
    usage = (
        "Try:\n" + "\n".join(f"- {example}" for example in suggestions)
        if suggestions
        else help_text
    )
    return "\n\n".join(part for part in (about, usage) if part)


async def announce_spawn_introduction(actor: Actor, introduction: str) -> None:
    """Send a first-start introduction from the new agent's own chat."""
    if not introduction or actor.state != ActorState.RUNNING:
        return
    try:
        await actor.notify_user(introduction)
    except Exception:
        logger.exception("[%s] Could not send the new agent's introduction", actor.name)
