# SPDX-License-Identifier: Apache-2.0
"""When an agent's card goes: when its withdrawal stands, by main's rule.

An agent that ends, by any path, withdraws its manifest: an empty payload on
``agents/<id>/manifest``. Anything that can publish under ``agents/`` can send
one, and the payload says nothing of who did, so the dashboard does not take it
as final. It follows main's decision instead
(`wactorz.agents.main.lifecycle.LifecycleService.withdrawal_decision`): one for
an agent running in this process is refused, one its node still lists waits for
that node's next heartbeat, and the rest stand. The decision is read when the
withdrawal arrives, and main tells this module of every one that stands, at once
or when it settles (its ``watchers``); only then is the card taken off, once.

Two checks are this module's own. An agent running in this process is never
taken off on a withdrawal, whatever else is known. And while the system starts,
or main restarts, there is no main to decide: a withdrawal is held, and handed
to main once it is back, through its manifest registry as one off the broker
would be, since main missed it too. Only a profile that runs no main, latched
when the system started, takes the rest as they come.
"""

import logging
from collections import OrderedDict
from typing import Any

from ..agents.lookup import find_main_actor
from ..agents.main.lifecycle import STANDS
from . import cost, events, runtime, ws

logger: logging.Logger = logging.getLogger(__name__)

#: The ids of withdrawals heard while there was no main to decide them, oldest first.
_held: OrderedDict[str, None] = OrderedDict()

#: The most withdrawals held at once; the oldest goes past it.
HELD_KEPT = 1000

#: The leaf a manifest is published on.
MANIFEST_LEAF = "manifest"


def withdrawn_agent(topic: str, payload: str) -> str:
    """The agent id ``topic`` and ``payload`` withdraw, or "" when they are not a withdrawal."""
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "agents" and parts[2] == MANIFEST_LEAF:
        return parts[1] if parts[1] and not payload.strip() else ""
    return ""


async def receive(agent_id: str) -> None:
    """Act on a withdrawal of ``agent_id`` heard on the broker."""
    if runtime.registry is not None and runtime.registry.get(agent_id) is not None:
        # Running here: an agent leaves the registry before it withdraws.
        return
    if runtime.started_with_main is False:
        await removed(agent_id)
        return
    main = _main()
    if runtime.started_with_main and main is not None and watch(main) is not None:
        # Main hears the same message on a client of its own and decides it, in
        # either order with this. Its decision is read here too, so one that
        # stands at once is acted on whether or not main has told its watchers
        # yet; one held or refused is left to main, whose watcher takes the card
        # off when it stands. Told twice, the card still goes once.
        await _if_it_stands(main, agent_id)
        return
    _held.pop(agent_id, None)
    _held[agent_id] = None
    while len(_held) > HELD_KEPT:
        dropped, _ = _held.popitem(last=False)
        logger.warning(
            "[withdrawals] Too many held until main can decide them; dropped %s's", dropped[:8]
        )


def holding() -> bool:
    """Whether any withdrawal is held, waiting for main."""
    return bool(_held)


async def release() -> None:
    """Hand main the withdrawals held while it was not there, watching it first.

    In a profile without main, latched as the system started, each is taken
    as it would have been on arrival.

    A main that has restarted is a new one, with watchers of its own, so it is
    watched again before anything is handed over: otherwise a withdrawal it
    accepts would never reach the dashboard.
    """
    if not _held or runtime.started_with_main is None:
        return
    if runtime.started_with_main is False:
        # The profile without main, latched only now: each is taken as any
        # withdrawal is there, an agent running here still refused.
        held = list(_held)
        _held.clear()
        for agent_id in held:
            await receive(agent_id)
        return
    main = _main()
    if main is None or watch(main) is None:
        return
    held = list(_held)
    _held.clear()
    for agent_id in held:
        try:
            await main.manifests.receive_manifest(f"agents/{agent_id}/{MANIFEST_LEAF}", b"")
        except Exception:
            logger.exception("[withdrawals] Main could not take %s's withdrawal", agent_id[:8])


def watch(main: Any) -> bool | None:
    """Follow ``main``'s withdrawals: True if watched only now, False if already, None if it cannot be.

    Idempotent, and cheap enough to ask on every message: a main that has
    restarted is a new one, and is watched from the first message after.
    """
    lifecycle = getattr(main, "lifecycle", None)
    watchers = getattr(lifecycle, "watchers", None)
    if not isinstance(watchers, list):
        return None
    if removed in watchers:
        return False
    watchers.append(removed)
    return True


async def _if_it_stands(main: Any, agent_id: str) -> None:
    """Take ``agent_id``'s card off if main's rule lets its withdrawal stand."""
    decide = getattr(getattr(main, "lifecycle", None), "withdrawal_decision", None)
    if not callable(decide):
        return
    decision = decide(agent_id)
    if getattr(decision, "verdict", "") == STANDS:
        await removed(agent_id)


async def removed(agent_id: str) -> None:
    """Take ``agent_id``'s card off: its withdrawal stands. Its frame goes out once."""
    frame = events.withdraw(agent_id)
    if frame is None or runtime.hard_resetting:
        return
    await broadcast_deleted(agent_id)


async def broadcast_deleted(agent_id: str) -> None:
    """Tell the browsers that ``agent_id``'s card is gone."""
    # A patch only adds and updates, so a snapshot that no longer names the
    # agent still leaves its card on screen. This is the frame that removes
    # it, and the same one the REST and WebSocket delete paths send.
    await cost.settled_totals()
    await ws.broadcast(
        {
            "type": events.DELETE_AGENT_FRAME,
            "agent_id": agent_id,
            "state": events.snapshot(),
        }
    )


def _main() -> Any:
    return find_main_actor(runtime.registry) if runtime.registry is not None else None
