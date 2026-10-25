"""What a reset clears from a running agent, wherever it runs.

A reset of the stored history, state or counters is half done while the agent
holding a copy in memory goes on running: its next turn, persist or heartbeat
puts back what was cleared. Main applies these to the agents in its process,
and a node to the agents it hosts, so the two cannot disagree about what a
reset of one kind takes.
"""

from typing import Any

#: The keys a *chat* reset owns, in the store and in an agent's own state.
CHAT_STATE_KEYS = ("conversation_history", "history_summary")

#: The keys a *metrics* reset owns: an agent's running cost and message count.
METRIC_STATE_KEYS = ("_final_cost", "_messages_processed")

#: The kinds of reset that reach into a running agent. The others -- the spawn
#: registry, the log files, a factory reset -- concern main's records, or remove
#: the agent altogether.
LIVE_SCOPES = ("chat", "state", "metrics")


def forget_legacy_state(actor: Any, keys: tuple[str, ...] | None = None) -> None:
    """Drop the in-memory state a wipe cannot otherwise reach.

    An actor without a persistence store holds its state in
    `_persistent_state` and writes all of it back on every `persist`, so a key
    a reset removed from disk returns with the next write unless it goes from
    memory too. An actor with a store holds nothing there, and this leaves it
    unchanged.

    `keys=None` clears the lot, for a reset of the agent's whole state: the file
    is deleted, so anything left in memory is the two halves disagreeing. A chat
    reset passes `CHAT_STATE_KEYS` instead, because it clears the conversation
    and nothing else.
    """
    state = getattr(actor, "_persistent_state", None)
    if not isinstance(state, dict):
        return
    if keys is None:
        state.clear()
        return
    for key in keys:
        state.pop(key, None)


def reset_actor_cost(actor: Any) -> None:
    """Zero a live actor's cost/token counters AND its per-call accrual baseline.

    The baseline must move with total_cost_usd: _persist_cost / _accrue_usage
    accumulate (total_cost_usd - baseline) into the global period and all-time
    counters. Zeroing total_cost_usd on a reset without also zeroing the baseline
    leaves the baseline at the pre-reset total, so every subsequent call yields a
    negative delta and the period/all-time counters stop advancing until spend
    climbs back past the old total.
    """
    if not hasattr(actor, "total_cost_usd"):
        return
    actor.total_cost_usd = 0.0
    actor.total_input_tokens = 0
    actor.total_output_tokens = 0
    if hasattr(actor, "_last_persisted_usd"):
        actor._last_persisted_usd = 0.0
    if hasattr(actor, "_last_period_cost_usd"):
        actor._last_period_cost_usd = 0.0


def forget_conversation(actor: Any) -> None:
    """Clear what the agent remembers of its conversation, in memory.

    Without it the agent still knows the conversation the store no longer has,
    and writes it back on its next turn.
    """
    if hasattr(actor, "_conversation_history"):
        actor._conversation_history = []
    if hasattr(actor, "_history_summary"):
        actor._history_summary = ""
    forget_legacy_state(actor, CHAT_STATE_KEYS)


def zero_counters(actor: Any) -> None:
    """Zero the agent's message count and cost, so its next heartbeat reports the reset."""
    actor.metrics.messages_processed = 0
    reset_actor_cost(actor)
    forget_legacy_state(actor, METRIC_STATE_KEYS)


def reset_live(actor: Any, scope: str) -> None:
    """Clear one of the `LIVE_SCOPES` from a running agent, in memory."""
    if scope == "chat":
        forget_conversation(actor)
    elif scope == "state":
        forget_legacy_state(actor)
    elif scope == "metrics":
        zero_counters(actor)
