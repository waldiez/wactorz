# SPDX-License-Identifier: Apache-2.0
"""Moving a model's pointers from chat, and saying what a version is judged on.

The pointers belong to the store (`wactorz.core.artifacts`); these verbs move
them as `wactorz-artifacts` does, with ``by`` saying the move came from chat,
and then have main publish where they stand, so the move is heard at once
rather than at the watcher's next look. Listing stays with `/artifacts` in
`info`, which only reads.
"""

import asyncio

from ....core.artifacts import ArtifactError, ArtifactStore, Pointer
from ....core.judgement import JudgementError, declared
from ....core.paths import resolve_state_dir
from ....core.promotion import FIELDS, PolicyError
from ....core.promotion import declared as policy_declared
from .dispatch import CommandContext, command
from .groups import MODELS

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
    group=MODELS,
    forms=(("/artifacts activate <name:version> [reason]", "make a version the one agents serve"),),
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
    group=MODELS,
    forms=(("/artifacts shadow <name:version> [reason]", "try a version beside the active one"),),
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
    group=MODELS,
    forms=(("/artifacts promote <name> [reason]", "make the shadow version active"),),
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
    group=MODELS,
    forms=(("/artifacts clear active|shadow <name> [reason]", "unset a pointer"),),
)
async def clear_model_pointer(ctx: CommandContext, argument: str) -> str:
    """``/artifacts clear active|shadow <name> [reason]``."""
    which, rest = _split(argument)
    name, reason = _split(rest or "")
    if not name:
        return "Usage: /artifacts clear active|shadow <name> [reason]"
    try:
        was = await asyncio.to_thread(_store().clear, which, name, by=BY, reason=reason)
    except ArtifactError as exc:
        return f"Cannot clear {which!r} of {name!r}: {exc}"
    await _announce(ctx)
    return f"{was.name} has no {was.pointer} version now (was {was.reference})."


#: The word that says a version is judged on nothing.
NOTHING = "none"

JUDGE_USAGE = (
    "Usage: /artifacts judge <name:version> <value>[,<value>...] [label source]"
    f" -- or {NOTHING} instead of the values. This judges it as a detector; a classifier or"
    " a regressor is declared with wactorz-artifacts judge --kind ... --field ..."
)


@command(
    "/artifacts judge",
    prefixes=("/artifacts judge ",),
    summary="say what a model version is judged on",
    group=MODELS,
    forms=(
        (
            "/artifacts judge <name:version> <value>[,<value>...] [label source]",
            "the label values that count as what a detector should flag"
            " (a classifier or regressor: wactorz-artifacts judge --kind ... --field ...)",
        ),
        (f"/artifacts judge <name:version> {NOTHING}", "judge it on nothing"),
    ),
)
async def judge_model(ctx: CommandContext, argument: str) -> str:
    """``/artifacts judge <name:version> <value>[,<value>...] [label source]``."""
    reference, rest = _split(argument)
    values, source = _split(rest or "")
    if not reference or not values:
        return JUDGE_USAGE
    try:
        judgement = None if values == NOTHING else declared(positive=values, source=source, by=BY)
        judged = await asyncio.to_thread(_store().judge, reference, judgement)
    except (ArtifactError, JudgementError) as exc:
        return f"Cannot judge {reference!r}: {exc}"
    said = judged.judgement
    if said is None:
        return f"{judged.reference} is judged on nothing now."
    where = f"the labels of {said.source}" if said.source else "each input's own source"
    return (
        f"{judged.reference} is judged on {', '.join(said.positive)} as positive, against {where}."
    )


POLICY_USAGE = (
    "Usage: /artifacts policy <name> <field>=<value> ... -- fields: "
    + ", ".join(FIELDS)
    + f"; or {NOTHING} to unset it"
)


def _policy_fields(words: str) -> dict[str, object]:
    """``metric=f1 margin=0.02 automatic=yes`` as the fields a policy is given."""
    fields: dict[str, object] = {}
    for word in words.split():
        name, sep, value = word.partition("=")
        if not sep:
            raise PolicyError.unknown_field(word)
        if name in ("margin", "min_days"):
            fields[name] = float(value)
        elif name == "min_labelled":
            fields[name] = int(value)
        else:
            fields[name] = value
    return fields


@command(
    "/artifacts policy",
    prefixes=("/artifacts policy ",),
    summary="say when a model's shadow may become active",
    group=MODELS,
    forms=(
        (
            "/artifacts policy <name> <field>=<value> ...",
            "metric, margin, min_days, min_labelled, significant, automatic",
        ),
        (f"/artifacts policy <name> {NOTHING}", "unset the policy"),
    ),
)
async def policy_model(ctx: CommandContext, argument: str) -> str:
    """``/artifacts policy <name> metric=f1 margin=0.02 min_days=1 min_labelled=100 significant=yes automatic=no``."""
    name, rest = _split(argument)
    if not name or not rest:
        return POLICY_USAGE
    store = _store()
    try:
        if rest == NOTHING:
            was = await asyncio.to_thread(store.clear_policy, name)
            return f"{name} has no policy now." + (f" It was: {was.words()}." if was else "")
        policy = policy_declared(_policy_fields(rest), over=store.policy(name), by=BY)
        set_to = await asyncio.to_thread(store.set_policy, name, policy)
    except (ArtifactError, PolicyError, ValueError) as exc:
        return f"Cannot set the policy of {name!r}: {exc}"
    return f"{name} is promoted when its shadow shows {set_to.words()}."
