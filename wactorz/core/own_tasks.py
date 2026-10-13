"""Telling the system's tasks from a host's, on a loop the two share.

A program that embeds Wactorz lends it an event loop: a notebook's, a web
framework's. At shutdown the system stops every task it started, and must
leave the host's alone, whenever they were created. The tasks cannot be told
apart by when they appeared, so they are marked as they are made: while a run
is on, a task factory on the loop records every task created from within the
run's own context, which each of its tasks inherits, and a host's context
never carries.
"""

import asyncio
import contextvars
import weakref
from collections.abc import Callable
from typing import Any

#: True in the context of a run and of every task it creates.
_in_run: contextvars.ContextVar[bool] = contextvars.ContextVar("wactorz_in_run", default=False)


class TaskTagger:
    """Marks the tasks a run creates, for the duration of that run."""

    def __init__(self) -> None:
        self._tasks: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()
        self._previous: Callable[..., Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._token: contextvars.Token[bool] | None = None

    def install(self) -> None:
        """Start marking. Called from the run's own task, whose context it marks."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._previous = loop.get_task_factory()
        loop.set_task_factory(self._factory)
        self._token = _in_run.set(True)

    def uninstall(self) -> None:
        """Stop marking: the loop's previous factory and the context are put back."""
        if self._loop is not None:
            self._loop.set_task_factory(self._previous)
        if self._token is not None:
            _in_run.reset(self._token)
        self._loop = self._previous = self._token = None

    def _factory(self, loop: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> Any:
        # Whatever the loop would have done without us, then the mark. The
        # keyword arguments are the loop's own (a name, a context) and differ
        # between Python versions, so they are passed through untouched.
        if self._previous is not None:
            task = self._previous(loop, coro, **kwargs)
        else:
            task = asyncio.Task(coro, loop=loop, **kwargs)
        if _in_run.get():
            self._tasks.add(task)
        return task

    def tasks(self) -> set[asyncio.Task[Any]]:
        """The run's tasks that have not finished."""
        return {task for task in self._tasks if not task.done()}


#: The tagger of the run in progress, or ``None`` outside one.
_active: TaskTagger | None = None


def start() -> TaskTagger:
    """Begin a run's marking; see :class:`TaskTagger`."""
    global _active
    tagger = TaskTagger()
    tagger.install()
    _active = tagger
    return tagger


def stop(tagger: TaskTagger) -> None:
    """End it."""
    global _active
    tagger.uninstall()
    if _active is tagger:
        _active = None


def own_tasks() -> set[asyncio.Task[Any]]:
    """The tasks a shutdown may stop: the run's, or every task when no run marks its own."""
    if _active is not None:
        return _active.tasks()
    return set(asyncio.all_tasks())
