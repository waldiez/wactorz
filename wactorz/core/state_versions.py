"""Bringing an agent's stored state up to the version its code expects.

An agent declares the shape of what it persists with a version number and a
function that upgrades one version to the next. A native actor sets
``state_version`` and defines ``upgrade_state(state, from_version)``; generated
and catalogue code defines ``STATE_VERSION`` and ``upgrade_state`` at module
level. The version the state is at is kept in the state itself, under
``STATE_VERSION_KEY``, so it travels with the agent when it migrates and reads
the same from main's pickle and from a node's JSON. State with no version is at
version 0.

When the agent starts, each step from the stored version to the declared one
runs in order, on a copy, and the result is written only once every step has
succeeded. A step that fails leaves what is stored as it was and stops the
agent starting: code that cannot read its own state would only fail later, and
further from the cause. State newer than the code is left alone, since older
code has no business rewriting data it does not understand.
"""

import copy
import inspect
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Where an agent's state records the version it is at.
STATE_VERSION_KEY = "_state_version"

#: ``upgrade_state(state, from_version)``: the state at ``from_version + 1``.
#: May be a coroutine function.
Upgrade = Callable[[dict[str, Any], int], Any]


class StateUpgradeError(RuntimeError):
    """A step of an agent's state upgrade failed, and nothing was written."""

    def __init__(self, agent: str, from_version: int, cause: BaseException) -> None:
        super().__init__(
            f"'{agent}' could not upgrade its state from version {from_version} to "
            f"{from_version + 1}: {type(cause).__name__}: {cause}. What it had stored is "
            f"left as it was."
        )
        self.from_version = from_version


def declared_version(value: Any) -> int | None:
    """``value`` as a state version, or None when it is not one an agent may declare."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def stored_version(state: dict[str, Any]) -> int:
    """The version ``state`` is at: 0 when it carries none, or one that is not a version."""
    return declared_version(state.get(STATE_VERSION_KEY)) or 0


async def upgraded(
    agent: str, state: dict[str, Any], declared: int, upgrade: Upgrade | None
) -> dict[str, Any] | None:
    """``state`` brought up to ``declared``, or None when there is nothing to write.

    Raises `StateUpgradeError` naming the step that failed. Without an
    ``upgrade`` function, or with no state to upgrade, the version is only
    stamped: the agent has said its state needs nothing changed.
    """
    stored = stored_version(state)
    if stored == declared:
        return None
    if stored > declared:
        logger.warning(
            "[%s] Its state is at version %s and its code expects %s; left as it is, "
            "since older code cannot know what the newer version changed.",
            agent,
            stored,
            declared,
        )
        return None
    body = _copied({k: v for k, v in state.items() if k != STATE_VERSION_KEY})
    if upgrade is None or not body:
        body[STATE_VERSION_KEY] = declared
        logger.info("[%s] State marked as version %s; nothing to upgrade.", agent, declared)
        return body
    for version in range(stored, declared):
        body = await _one_step(agent, body, version, upgrade)
    body[STATE_VERSION_KEY] = declared
    logger.info("[%s] State upgraded from version %s to %s.", agent, stored, declared)
    return body


async def _one_step(
    agent: str, state: dict[str, Any], version: int, upgrade: Upgrade
) -> dict[str, Any]:
    try:
        result = upgrade(state, version)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:
        raise StateUpgradeError(agent, version, exc) from exc
    if not isinstance(result, dict):
        raise StateUpgradeError(
            agent, version, TypeError(f"upgrade_state returned {type(result).__name__}, not a dict")
        )
    return result


def _copied(state: dict[str, Any]) -> dict[str, Any]:
    """A copy an upgrade may change freely, so a failed step changes nothing stored.

    Deep where the values allow it. A value that cannot be copied (an open
    handle, some model objects) is shared instead, and only the dict is new.
    """
    try:
        return copy.deepcopy(state)
    except Exception:
        return dict(state)
