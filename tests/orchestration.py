"""The seam between the chat surfaces and whatever answers them.

A person types somewhere -- the dashboard, the REST API, the terminal, Discord,
Telegram, WhatsApp -- and something decides what the words mean and answers.
That something is an :class:`Orchestrator`. Main, the model-driven one, is the
default; a deployment without a model gets a model-free one; a developer can
supply their own, a LangGraph graph or a rule engine, say. The surfaces call
the orchestrator and never ask which one is behind the call.

Three things cross the seam, and nothing else:

* ``handle_turn(text, channel=..., user=...)`` answers one message in full.
* ``handle_turn_stream(...)`` answers it in pieces, as a model produces them,
  for a surface that shows words as they arrive.
* ``commands()`` names the slash commands the orchestrator answers, so a
  surface can say "unknown command" for the rest instead of forwarding it.

The ``channel`` says where the message came from, which is a matter of trust
rather than of transport: the dashboard, the terminal and the REST API are the
operator's own, while a social channel is a public endpoint that anyone who
finds it can talk to. Main answers the latter through its restricted path --
conversation and device control, no spawning, deleting or code -- and an
orchestrator of your own should draw the same line. ``user`` is the sender
where the surface knows it (a Discord or Telegram id); the dashboard does not.

Lifecycle commands that genuinely need main -- deploying a node, moving an
agent, the beta banner from main's manifests -- stay with main; the seam is for
the turn itself.
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from .agents.lookup import MAIN_ACTOR_NAME
from .agents.main.commands import registry as main_command_registry
from .agents.main.commands.dispatch import REWRITES
from .core.actor import ActorState
from .core.turns import acting_as

if TYPE_CHECKING:
    from .agents.main import MainActor
    from .core.registry import ActorRegistry

#: Where a message came from. The first three are the operator's own surfaces;
#: anything else is treated as :data:`SOCIAL`, a public endpoint, and answered
#: with the restrictions that go with one.
DASHBOARD = "dashboard"
CLI = "cli"
REST = "rest"
SOCIAL = "social"

#: The channels whose sender is the operator. A channel not named here is
#: answered as a social one: fail closed, since a new surface that forgot to
#: say which it is should not be handed the admin commands by default.
TRUSTED_CHANNELS = frozenset({DASHBOARD, CLI, REST})


def is_trusted(channel: str) -> bool:
    """Whether a message from ``channel`` may reach the admin surface."""
    return channel in TRUSTED_CHANNELS


@runtime_checkable
class Orchestrator(Protocol):
    """What answers a chat turn.

    Implement the three methods and hand an instance to ``run(orchestrator=...)``
    or name it in ``WACTORZ_ORCHESTRATOR``; every chat surface then talks to it.
    ``handle_turn_stream`` is an async generator of text chunks; an orchestrator
    that has nothing to stream yields its whole answer once.
    """

    async def handle_turn(self, text: str, *, channel: str, user: str | None = None) -> str:
        """Answer one message from a person, in full."""
        ...

    def handle_turn_stream(
        self,
        text: str,
        *,
        channel: str,
        user: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        """Answer one message in pieces, as they are produced.

        ``attachments`` are content blocks for files sent with the message, in
        the shape :func:`wactorz.agents.llm.attachments.to_blocks` produces; an
        orchestrator that cannot read them ignores them.
        """
        ...

    def commands(self) -> frozenset[str]:
        """The slash commands this orchestrator answers, by their first word (``/help``)."""
        ...


def _first_words(spellings: tuple[str, ...]) -> set[str]:
    """The slash commands among ``spellings``, each cut to its first word."""
    return {s.split()[0] for s in spellings if s.startswith("/") and s.split()}


def main_commands() -> frozenset[str]:
    """Every slash command main answers, by first word, short forms included."""
    names: set[str] = set()
    for command in main_command_registry:
        names |= _first_words((command.name, *command.exact, *command.prefixes))
    names |= _first_words(tuple(short for short, _full in REWRITES))
    return frozenset(names)


async def _labelled(chunks: AsyncIterator[Any], agent: str) -> AsyncIterator[Any]:
    """``chunks``, with each step of the generator run as ``agent``'s work.

    A context variable set inside an async generator stays set in the consumer
    between yields and is never reset if the consumer stops early, so the
    label goes around each step rather than around the whole iteration.
    """
    while True:
        with acting_as(agent):
            try:
                chunk = await chunks.__anext__()
            except StopAsyncIteration:
                return
        yield chunk


#: Why main cannot answer, by the state that says so.
_UNAVAILABLE = {
    ActorState.STOPPED.value: "is stopped. Start it to send messages.",
    ActorState.FAILED.value: "has failed. It should restart shortly.",
}


class MainOrchestrator:
    """Main, the model-driven orchestrator, behind the seam.

    Looks main up in the registry on every turn rather than holding the actor:
    main is supervised, and a restart puts a new instance under the same name.
    A trusted channel reaches ``process_user_input`` (``process_user_input_stream``
    when streaming); any other channel reaches ``process_user_input_restricted``,
    whose answer a streaming caller receives as one chunk, since the restricted
    path does not stream.
    """

    def __init__(self, registry: "ActorRegistry") -> None:
        self._registry = registry

    def _main(self) -> "MainActor":
        actor = self._registry.find_by_name(MAIN_ACTOR_NAME)
        if actor is None:
            raise LookupError(f"{MAIN_ACTOR_NAME} is not running")
        # The entry points below reach into main directly rather than through
        # its mailbox, so a state that ends the mailbox is checked here, or a
        # failed main would go on answering until its replacement registers.
        # By value: ActorState is a str enum, compared by value everywhere.
        state = getattr(getattr(actor, "state", None), "value", "")
        reason = _UNAVAILABLE.get(str(state))
        if reason is not None:
            raise LookupError(f"{MAIN_ACTOR_NAME} {reason}")
        # Whatever is registered under main's name is main: the name is reserved
        # for it, and the three entry points used here are what define it.
        return cast("MainActor", actor)

    async def handle_turn(self, text: str, *, channel: str, user: str | None = None) -> str:
        main = self._main()
        if is_trusted(channel):
            return await main.process_user_input(text)
        return await main.process_user_input_restricted(text)

    async def handle_turn_stream(
        self,
        text: str,
        *,
        channel: str,
        user: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        main = self._main()
        if not is_trusted(channel):
            yield await main.process_user_input_restricted(text)
            return
        # Main's stream ends with a dict summarising the turn, which is for the
        # callers that read it off main directly; the seam carries words only.
        chunks = main.process_user_input_stream(text, attachments=attachments)
        async for chunk in _labelled(chunks, main.name):
            if isinstance(chunk, dict):
                continue
            yield str(chunk)

    def commands(self) -> frozenset[str]:
        return main_commands()


__all__ = [
    "CLI",
    "DASHBOARD",
    "REST",
    "SOCIAL",
    "TRUSTED_CHANNELS",
    "MainOrchestrator",
    "Orchestrator",
    "is_trusted",
    "main_commands",
]
