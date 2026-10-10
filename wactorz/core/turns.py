"""Which chat turn, and which agent, the code running now belongs to.

A turn is one message a person sent and everything done to answer it: main
deciding what it is, a planner, the agents it asks, their model and Home
Assistant calls. Its id is minted where the message enters and travels with
the work, so the log lines of one slow or failed answer can be found together,
whichever actor wrote them.

It travels three ways, none of which needs a caller to pass it on:

* **In context.** It lives in a context variable, which a task started inside
  the turn inherits, so model calls, Home Assistant calls and helper tasks
  carry it without being told.
* **In messages.** A :class:`~wactorz.core.actor.Message` takes the turn it is
  created in, and the actor that handles it works inside that turn.
* **Across the broker.** A task sent to an agent on a node names its turn, and
  the node works inside it.

The agent is tracked the same way, so a log line says which actor was at work
when it was written, whatever logger wrote it.
"""

import contextlib
import contextvars
import uuid
from collections.abc import Awaitable, Callable, Iterator
from typing import TypeVar

_turn: contextvars.ContextVar[str] = contextvars.ContextVar("wactorz_turn", default="")
_agent: contextvars.ContextVar[str] = contextvars.ContextVar("wactorz_agent", default="")

T = TypeVar("T")

#: Where a task sent over the broker names the turn it belongs to.
TURN_KEY = "_turn"


def is_task_topic(topic: str) -> bool:
    """Whether ``topic`` carries a task to an agent by name, as nodes receive them."""
    return topic.startswith("agents/by-name/") and topic.endswith("/task")


def turn_of(payload: object) -> str:
    """The turn a task that came over the broker belongs to, or "" if it names none."""
    if isinstance(payload, dict):
        turn = payload.get(TURN_KEY)
        if isinstance(turn, str):
            return turn
    return ""


def current_turn() -> str:
    """The turn the running code belongs to, or "" outside one."""
    return _turn.get()


def current_agent() -> str:
    """The agent the running code is working for, or "" outside one."""
    return _agent.get()


def new_turn_id() -> str:
    """A fresh turn id: short enough to read in a log line, unique enough for a day's turns."""
    return uuid.uuid4().hex[:12]


@contextlib.contextmanager
def turn_scope(turn_id: str = "") -> Iterator[str]:
    """Run inside a turn: ``turn_id`` if given, else the one running, else a new one.

    Every place a message can enter calls this, and several are nested -- the
    dashboard's chat route hands the message to main, which is also where the
    other interfaces come in -- so an inner one keeps the turn an outer one
    started rather than starting another.
    """
    running = _turn.get()
    chosen = turn_id or running or new_turn_id()
    if chosen == running:
        yield chosen
        return
    token = _turn.set(chosen)
    try:
        yield chosen
    finally:
        _turn.reset(token)


def begin_turn() -> str:
    """Start a new turn in the running task, replacing any, and return its id.

    For a task that takes one message after another for as long as it runs, a
    command line reading input say, where a ``with turn_scope()`` around each
    message would wrap the whole loop body. The next call replaces it, so it
    never outlives the message it was started for by more than the wait for the
    next one.
    """
    turn_id = new_turn_id()
    _turn.set(turn_id)
    return turn_id


@contextlib.contextmanager
def working_on(turn_id: str, agent: str) -> Iterator[None]:
    """Do an agent's work for one message: inside that message's turn, as that agent.

    The turn is set even when it is "", so a message that belongs to no turn is
    not handled inside whichever turn the previous one left behind.
    """
    turn_token = _turn.set(turn_id)
    agent_token = _agent.set(agent)
    try:
        yield
    finally:
        _agent.reset(agent_token)
        _turn.reset(turn_token)


@contextlib.contextmanager
def acting_as(agent: str) -> Iterator[None]:
    """Do the work inside as ``agent``'s, in whatever turn is running.

    For an agent's work that does not come through its mailbox: main answering
    a chat message, or the dashboard handing one straight to an agent.
    """
    token = _agent.set(agent)
    try:
        yield
    finally:
        _agent.reset(token)


def as_agent(agent: str) -> None:
    """Mark the running task as an agent's own, outside any turn, for the whole of its life.

    For a task an agent starts and keeps, such as a loop: what it logs is the
    agent's, and belongs to no turn -- a loop started while answering one would
    otherwise carry that turn into everything it does from then on. Set once at
    the top of that task and never reset, because the setting belongs to the
    task and ends with it.
    """
    _turn.set("")
    if agent:
        _agent.set(agent)


async def outside_any_turn(work: Callable[[], Awaitable[T]], agent: str = "") -> T:
    """Run ``work()`` as ``agent``'s own and outside any turn, in a task started for it.

    Wrap a long-lived task's work with this where the task is created: a task
    inherits the turn it is started in, and a loop started while answering a
    message would otherwise wear that turn for ever. Given the function rather
    than its coroutine, so a task cancelled before it first runs leaves no
    coroutine behind that was made and never awaited.
    """
    as_agent(agent)
    return await work()
