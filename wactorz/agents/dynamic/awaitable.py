"""Results that can be awaited without doing anything.

Several agent API calls are synchronous but get `await`ed anyway by generated
code. Returning one of these instead means the stray await is harmless rather
than a TypeError the model then tries to repair.

The history queries need the same for a second reason: where main keeps the
database they answer at once, and on a node they cross the broker and must be
awaited. Code that awaits them runs in both places, so on main their results
are awaitable too -- a list, a dict or a pandas frame that awaits to itself.
"""

import functools
from typing import Any


class AwaitableNone:
    """Sentinel that can be safely awaited (returns None) or used in bool context (False).

    LLMs writing async code inside DynamicAgent frequently add `await` to sync API
    calls like agent.subscribe(), agent.window(), agent.persist(), etc.  Returning
    this instead of bare None prevents 'TypeError: object NoneType can't be used
    in await expression' — the #1 runtime failure in LLM-generated agent code.
    """

    def __await__(self) -> Any:
        return iter([])  # completes immediately, yields None

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "None"


AWAITABLE_NONE = AwaitableNone()


def _itself(value: Any) -> Any:
    """A generator that finishes at once with ``value``: what ``__await__`` returns."""
    return value
    yield  # makes this a generator, so awaiting it completes without suspending


class AwaitableList(list):
    """A list that awaits to itself."""

    def __await__(self) -> Any:
        return _itself(self)


class AwaitableDict(dict):
    """A dict that awaits to itself."""

    def __await__(self) -> Any:
        return _itself(self)


@functools.cache
def _awaitable_frame_type(frame: type) -> type:
    """A subclass of ``frame``, pandas' DataFrame, that awaits to itself, built once.

    A frame's own operations return plain DataFrames, so only the frame a query
    hands back is awaitable.
    """

    class AwaitableFrame(frame):
        def __await__(self) -> Any:
            return _itself(self)

    return AwaitableFrame


def awaitable_frame(rows: list[dict[str, Any]]) -> Any:
    """``rows`` as a pandas DataFrame that awaits to itself. Raises ImportError without pandas.

    pandas is imported on every call, not only when the class is first built:
    whether it can be imported is asked each time, so the answer is never one
    remembered from an earlier call.
    """
    import pandas as pd  # optional dependency: the `data` extra

    return _awaitable_frame_type(pd.DataFrame)(rows)
