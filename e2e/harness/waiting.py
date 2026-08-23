"""Waiting on conditions, which is the only kind of waiting this suite has.

Three shapes, and the difference between them is the assertion they support:

``until``            something becomes true within a deadline.
``holds_for``        something stays true across a window - the shape that
                     catches a state that flickers into place and back out.
``becomes_and_stays``  both, in order: settles, then stays settled.

The last two look like sleeps and are not. A sleep asserts nothing; these fail,
by name, on the tick where the condition stopped holding. The distinction matters
because "the agent reached running" and "the agent reached running and was still
there a second later" are different claims, and a supervisor that restarts a
crashing agent forever satisfies only the first.

Every wait carries `what` - a phrase completing "timed out waiting for ...". A
timeout with no subject is a failure that costs someone half an hour to place.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

#: How long a condition is given before it is called a failure. Deliberately
#: generous: this suite starts real processes, and a slow machine is not a bug.
DEFAULT_TIMEOUT = 30.0

#: How often a condition is re-checked. Small enough that a scenario is not
#: paying a tenth of a second for something that was already true.
DEFAULT_INTERVAL = 0.05

#: How long "stays" means, absent a scenario saying otherwise.
DEFAULT_WINDOW = 1.5


class ConditionTimeout(AssertionError):
    """A condition never became true. An assertion, so pytest reports it as one."""


def until(
    condition: Callable[[], T],
    *,
    what: str,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
) -> T:
    """Poll until `condition` returns something truthy, and return it.

    Returns the value rather than a bool so the wait and the read are one step:
    ``agent = until(lambda: api.agent("weather"), what="the weather agent to exist")``
    leaves the caller holding the thing it waited for, with no second fetch that
    could observe a different moment.

    An exception from `condition` is treated as "not yet" until the deadline, and
    re-raised once it passes. A REST probe against a backend that is still binding
    raises rather than returning falsey, and that is a normal part of waiting for
    it - but an exception that never stops happening is the actual failure and
    must not be reported as a bare timeout.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            value = condition()
            if value:
                return value
            last_error = None
        except Exception as exc:  # - re-raised below if it outlives the deadline
            last_error = exc
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)
    if last_error is not None:
        raise ConditionTimeout(
            f"timed out after {timeout:g}s waiting for {what}; "
            f"the last attempt raised {type(last_error).__name__}: {last_error}"
        ) from last_error
    raise ConditionTimeout(f"timed out after {timeout:g}s waiting for {what}")


def holds_for(
    condition: Callable[[], object],
    *,
    what: str,
    window: float = DEFAULT_WINDOW,
    interval: float = DEFAULT_INTERVAL,
) -> None:
    """Fail if `condition` stops being true at any point across `window`.

    For claims a single check cannot make. "The agent is running" is true of an
    agent that is being restarted twice a second; "the agent was running for the
    whole of the last second and a half" is not.
    """
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        if not condition():
            raise ConditionTimeout(
                f"{what} stopped holding "
                f"{window - (deadline - time.monotonic()):.2f}s into a {window:g}s window"
            )
        time.sleep(interval)


def becomes_and_stays(
    condition: Callable[[], object],
    *,
    what: str,
    timeout: float = DEFAULT_TIMEOUT,
    window: float = DEFAULT_WINDOW,
    interval: float = DEFAULT_INTERVAL,
) -> None:
    """Wait for `condition`, then require it to keep holding.

    The shape for anything supervised. A crash-looping agent passes ``until`` on
    whichever poll catches it up, and fails here.
    """
    until(condition, what=what, timeout=timeout, interval=interval)
    holds_for(condition, what=what, window=window, interval=interval)


def dwell(seconds: float) -> None:
    """Linger, for the camera.

    The one place in this suite where waiting is not on a condition, and it is
    reached only through a profile that returns a non-zero number - `test` maps
    every dwell to 0.0, so this is a no-op under the profile that runs in CI.
    Not for use as a wait: nothing after it may depend on it having been long
    enough.
    """
    if seconds > 0:
        time.sleep(seconds)
