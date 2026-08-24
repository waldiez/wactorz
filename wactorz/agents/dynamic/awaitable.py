"""A sentinel that can be awaited without doing anything.

Several agent API calls are synchronous but get `await`ed anyway by generated
code. Returning this instead of None means the stray await is harmless rather
than a TypeError the model then tries to repair.
"""

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
