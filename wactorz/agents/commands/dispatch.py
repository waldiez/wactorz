"""Finding the command a message calls, and what a handler may reach.

Matching is on the whole stripped message, not on a leading slash. Several
commands answer to spellings that have none — `help`, `rules`, `list_nodes` —
so a matcher keyed on the first character would drop them.

**Registration order is dispatch order.** The chain this replaces encoded
precedence in its sequence: an exact match sits ahead of a prefix that would
also match, and a rewrite is expanded before the family it expands into. A
registry iterates in the order entries were added, so that order has to be
chosen deliberately rather than inherited from however imports happen to run.

A command is looked up here and run by the caller. Nothing in this package
reaches for an actor, a broker or a registry: what a handler needs arrives as a
`CommandContext`, which keeps the handlers testable without any of it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

#: Short forms, and the commands they stand for.
#:
#: Expanded before matching, so the shorter spelling is exactly the longer one
#: and cannot answer differently. Kept here rather than on either command: it is
#: a fact about the pair, and splitting it across the two is how they drift.
REWRITES: tuple[tuple[str, str], ...] = (
    ("/delete ", "/agents delete "),
    ("/stop ", "/agents stop "),
)

#: Trailing characters stripped before matching.
#:
#: A model writing `/help()` as though it were a function should still reach the
#: command rather than the conversation path.
_CALL_SYNTAX = "()"


class UndescribedCommand(ValueError):
    """Raised when a handler is registered without `@command` metadata.

    At import time rather than at dispatch: a command that was registered but
    never described would simply never match, which looks like the command
    having been removed.
    """

    def __init__(self, handler: object) -> None:
        name = getattr(handler, "__name__", repr(handler))
        super().__init__(f"{name} was never described by @command")


@dataclass(frozen=True)
class Command:
    """One command, and every spelling that reaches it.

    `exact` are spellings that take no argument; `prefixes` are those that do,
    and the rest of the message becomes the argument. A command can have both:
    `/agents` lists everything, `/agents weather` filters.
    """

    name: str
    handler: Callable[[CommandContext, str], Awaitable[str]]
    exact: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()
    summary: str = ""
    #: Whether a social channel may run this. Everything that spawns, deletes,
    #: runs code or changes automation is off by default.
    restricted_ok: bool = False
    #: Whether the prefix alone, with no argument, calls this command.
    #:
    #: Off by default, and that default is load-bearing. `/agents delete` with
    #: nothing after it is not a request to delete an agent named "" — it is an
    #: unfinished sentence, and a command that accepts it reports having deleted
    #: something. On means the handler answers with usage, which is what the
    #: lifecycle verbs did before and the only reason this is settable.
    bare_ok: bool = False

    def match(self, stripped: str) -> str | None:
        """The argument this command was given, or None if it was not called.

        An empty string means "called with no argument", which is different
        from None and is why the caller has to test for None rather than
        truthiness.
        """
        if stripped in self.exact:
            return ""
        for prefix in self.prefixes:
            if self.bare_ok and stripped == prefix.strip():
                return ""
            if stripped.startswith(prefix):
                return stripped[len(prefix) :].strip()
        return None


@dataclass
class CommandContext:
    """What a handler is allowed to reach.

    Deliberately narrow. A handler that needs something not here is a handler
    reaching past its job, and the missing member is worth seeing named rather
    than arriving through a whole actor.
    """

    #: The actor the command was typed at. Phase 1 keeps this while handlers
    #: still call back into it; each batch that moves a command replaces one
    #: more use with a named member above it.
    actor: Any
    #: True when the caller is a social channel, where the admin surface is
    #: refused at the action rather than by reading the text.
    restricted: bool = False
    #: Anything the handler wants to hand back to the caller besides its reply.
    extra: dict[str, Any] = field(default_factory=dict)


class CommandRegistry:
    """The commands, in the order they are tried."""

    def __init__(self) -> None:
        self._commands: list[Command] = []

    def add(self, handler: Callable[..., Any]) -> None:
        """Register a handler previously described by `@command`."""
        described = getattr(handler, "command", None)
        if described is None:
            raise UndescribedCommand(handler)
        self._commands.append(described)

    def __iter__(self) -> Any:
        return iter(self._commands)

    def __len__(self) -> int:
        return len(self._commands)

    def expand(self, stripped: str) -> str:
        """Rewrite a short form into the command it stands for.

        `/delete x` is `/agents delete x`, spelled shorter. The chain did this
        by rewriting the text and falling through to the block below, which
        meant the branch that matched was not the branch that answered. Here it
        is one step with the pairs in view, so a short form cannot drift away
        from what it expands to.

        Only the first match applies: an argument that happens to start with
        another short form is an argument, not a second command.
        """
        for short, full in REWRITES:
            if stripped.startswith(short):
                return full + stripped[len(short) :].strip()
        return stripped

    def find(self, text: str) -> tuple[Command, str] | None:
        """The command `text` calls and the argument it passes, or None.

        Returns the first match in registration order. Matching is
        case-sensitive: a command is something typed deliberately, and treating
        `Help` as one would also make `HELP ME WITH THIS` a command.
        """
        stripped = self.expand(text.strip().rstrip(_CALL_SYNTAX))
        for command in self._commands:
            argument = command.match(stripped)
            if argument is not None:
                return command, argument
        return None


#: The registry the actor dispatches through. Filled by the package's
#: `__init__`, which lists the commands in the order they are tried.
registry = CommandRegistry()


def command(
    name: str,
    *,
    exact: tuple[str, ...] = (),
    prefixes: tuple[str, ...] = (),
    summary: str = "",
    restricted_ok: bool = False,
    bare_ok: bool = False,
) -> Callable[
    [Callable[[CommandContext, str], Awaitable[str]]],
    Callable[[CommandContext, str], Awaitable[str]],
]:
    """Attach command metadata to a handler.

    Describing a command does not register it: the package's `__init__` does
    that, in an order it states. Keeping the two apart means importing a
    handler module has no effect on anything, so a test can import one without
    the registry changing underneath every other test in the run.
    """

    def describe(
        handler: Callable[[CommandContext, str], Awaitable[str]],
    ) -> Callable[[CommandContext, str], Awaitable[str]]:
        handler.command = Command(  # type: ignore[attr-defined]
            name=name,
            handler=handler,
            # The name is only a spelling when nothing else is given. A
            # command that takes an argument does not answer to its name
            # alone unless it says so with `bare_ok`, or `/agents delete`
            # would be a call to delete an agent named "".
            exact=exact or ((name,) if not prefixes else ()),
            prefixes=prefixes,
            summary=summary,
            restricted_ok=restricted_ok,
            bare_ok=bare_ok,
        )
        return handler

    return describe
