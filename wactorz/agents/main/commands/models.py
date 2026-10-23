"""Moving a model's pointers from chat: which version agents serve, and which is on trial.

The pointers belong to the store (`wactorz.core.artifacts`); these verbs move
them as `wactorz-artifacts` does, with ``by`` saying the move came from chat,
and then have main publish where they stand, so the move is heard at once
rather than at the watcher's next look. Listing stays with `/artifacts` in
`info`, which only reads.
"""

import asyncio

from ....core.artifacts import ArtifactError, ArtifactStore, Pointer
from ....core.paths import resolve_state_dir
from .dispatch import CommandContext, command

#: Who a move from chat is recorded as. Chat knows the channel a turn came
#: from, not a person's name, so the record says where, not who.
BY = "chat"


def _store() -> ArtifactStore:
    return ArtifactStore.in_state_dir(resolve_state_dir())


def _split(argument: str) -> tuple[str, str | None]:
    """The reference or name an argument starts with, and the rest as the reason."""
    target, _, reason = argument.strip().partition(" ")
    return target, reason.strip() or None


def _moved(pointer: Pointer) -> str:
    was = f" (was {pointer.name}:{pointer.previous})" if pointer.previous is not None else ""
    return f"{pointer.reference} is now {pointer.pointer}{was}."


async def _announce(ctx: CommandContext) -> None:
    await ctx.actor.artifact_service.publish_pointers()


@command(
    "/artifacts activate",
    prefixes=("/artifacts activate ",),
    summary="make a model version the one agents serve",
)
async def activate_model(ctx: CommandContext, argument: str) -> str:
    """``/artifacts activate <name:version> [reason]``."""
    reference, reason = _split(argument)
    try:
        moved = await asyncio.to_thread(_store().move, "active", reference, by=BY, reason=reason)
    except ArtifactError as exc:
        return f"Cannot activate {reference!r}: {exc}"
    await _announce(ctx)
    return _moved(moved)


@command(
    "/artifacts shadow",
    prefixes=("/artifacts shadow ",),
    summary="try a model version beside the active one",
)
async def shadow_model(ctx: CommandContext, argument: str) -> str:
    """``/artifacts shadow <name:version> [reason]``."""
    reference, reason = _split(argument)
    try:
        moved = await asyncio.to_thread(_store().move, "shadow", reference, by=BY, reason=reason)
    except ArtifactError as exc:
        return f"Cannot shadow {reference!r}: {exc}"
    await _announce(ctx)
    return _moved(moved)


@command(
    "/artifacts promote",
    prefixes=("/artifacts promote ",),
    summary="make a model's shadow version the active one",
)
async def promote_model(ctx: CommandContext, argument: str) -> str:
    """``/artifacts promote <name> [reason]``."""
    name, reason = _split(argument)
    try:
        moved = await asyncio.to_thread(_store().promote, name, by=BY, reason=reason)
    except ArtifactError as exc:
        return f"Cannot promote {name!r}: {exc}"
    await _announce(ctx)
    return _moved(moved)


@command(
    "/artifacts clear",
    prefixes=("/artifacts clear ",),
    summary="unset a model's active or shadow pointer",
)
async def clear_model_pointer(ctx: CommandContext, argument: str) -> str:
    """``/artifacts clear active|shadow <name>``."""
    which, name = _split(argument)
    if not name:
        return "Usage: /artifacts clear active|shadow <name>"
    try:
        was = await asyncio.to_thread(_store().clear, which, name)
    except ArtifactError as exc:
        return f"Cannot clear {which!r} of {name!r}: {exc}"
    await _announce(ctx)
    return f"{was.name} has no {was.pointer} version now (was {was.reference})."
