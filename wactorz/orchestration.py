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

import asyncio
from collections.abc import AsyncIterator, Iterable
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from .agents.lookup import MAIN_ACTOR_NAME
from .agents.main.commands import registry as main_command_registry
from .agents.main.commands.dispatch import REWRITES
from .core.actor import Actor, ActorState
from .core.task_text import reply_text, task_payload
from .core.turns import acting_as
from .errors import StartupError
from .plugins import resolve_target

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


#: How long the model-free orchestrator waits for an agent's reply. Generous on
#: purpose: an agent that scores a reading answers at once, one that trains or
#: fetches may not, and the bug being prevented is an unbounded wait.
DIRECT_ASK_TIMEOUT_S = 150.0

#: The slash commands the model-free orchestrator answers.
DIRECT_COMMANDS = frozenset({"/agents", "/topics", "/nodes", "/help"})

DIRECT_HELP = """\
No model runs in this profile, so chat reaches the agents directly.
  @<agent> {json}   send the JSON to an agent as its task; the reply is its return value
  @<agent> <text>   send text, which the agent receives as {"text": ...}
  /agents           the running agents and their state
  /topics           the topics they publish and listen to
  /nodes            this process is the one node
  /help             this help"""


def _names(actors: Iterable[Actor]) -> str:
    """The agents as a person addresses them, or a note that there are none."""
    return ", ".join(f"@{actor.name}" for actor in actors) or "(none)"


def _state_of(actor: Actor) -> str:
    state = getattr(actor, "state", None)
    return str(getattr(state, "value", state or "?"))


def _declared_topics(actor: Actor) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What ``actor`` says it listens to and publishes, as a function or a class declares it."""
    spec = getattr(actor, "spec", None)
    if spec is not None:
        publishes = (spec.publishes,) if spec.publishes else ()
        return tuple(spec.subscribes), publishes
    cls = type(actor)
    return _topic_tuple(getattr(cls, "SUBSCRIBES", ())), _topic_tuple(getattr(cls, "PUBLISHES", ()))


def _topic_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    return tuple(str(topic) for topic in value or ())


def _split_mention(text: str) -> tuple[str, str]:
    """``@name rest`` as ``(name, rest)``; ``("", text)`` when nothing is mentioned."""
    if not text.startswith("@"):
        return "", text
    name, _sep, rest = text[1:].partition(" ")
    return name, rest.strip()


class DirectOrchestrator:
    """Chat without a model: a message reaches the agent it names, and nothing else.

    What answers the minimal profile. ``@name {json}`` sends the JSON to that
    agent as its task and shows the reply, the way another agent's ``send_to``
    or :func:`wactorz.ask` would; ``@name text`` sends ``{"text": ...}``. A
    message that names no agent is answered with the running agents and how to
    address one, since there is no model to read it. The commands it serves
    are answered from the registry; the channel makes no difference, as there
    is no admin surface to protect.
    """

    def __init__(self, registry: "ActorRegistry", *, timeout: float = DIRECT_ASK_TIMEOUT_S) -> None:
        self._registry = registry
        self._timeout = timeout

    async def handle_turn(self, text: str, *, channel: str, user: str | None = None) -> str:
        stripped = text.strip()
        if stripped.startswith("/"):
            return self._command(stripped)
        name, rest = _split_mention(stripped)
        if name:
            return await self._ask(name, rest)
        return (
            f"No model runs in this profile, so there is nothing to read {stripped!r} with. "
            f"Address an agent directly.\n"
            f"Running: {_names(self._registry.all_actors())}\n"
            'Try: @<agent> {"key": "value"}, or /help.'
        )

    async def handle_turn_stream(
        self,
        text: str,
        *,
        channel: str,
        user: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        if attachments:
            yield "[note] attachments not sent: no model runs in this profile to read them.\n"
        yield await self.handle_turn(text, channel=channel, user=user)

    def commands(self) -> frozenset[str]:
        return DIRECT_COMMANDS

    async def _ask(self, name: str, rest: str) -> str:
        if not rest:
            return f"[usage] @{name} <text or {{json}}>"
        try:
            reply = await self._registry.ask(name, task_payload(rest), timeout=self._timeout)
        except LookupError:
            return (
                f"No agent called @{name} is running.\n"
                f"Running: {_names(self._registry.all_actors())}"
            )
        except asyncio.TimeoutError:
            return f"[error] @{name} did not reply within {self._timeout:g}s."
        except RuntimeError as exc:
            # An error reply, or a mailbox that would not take the task: the
            # agent's words, since it is the agent that is being talked to.
            return f"[error] @{name}: {exc}"
        return reply_text(reply)

    def _command(self, text: str) -> str:
        word = text.split()[0].rstrip("()")
        actors = self._registry.all_actors()
        if word == "/help":
            return f"{DIRECT_HELP}\nRunning: {_names(actors)}"
        if word == "/agents":
            if not actors:
                return "No agents running."
            lines = [f"  [{_state_of(a):8s}] @{a.name:<22s} {a.actor_id[:8]}" for a in actors]
            return "Agents:\n" + "\n".join(lines)
        if word == "/nodes":
            return f"Nodes:\n  local    online   {_names(actors)}"
        if word == "/topics":
            lines = []
            for actor in actors:
                subscribes, publishes = _declared_topics(actor)
                lines.extend(f"  {topic:40s} ← @{actor.name}" for topic in publishes)
                lines.extend(f"  {topic:40s} → @{actor.name}" for topic in subscribes)
            if not lines:
                return "No topics declared by the running agents."
            return "Topics (← published by, → listened to by):\n" + "\n".join(lines)
        return "Unknown command. Type /help for available commands."


#: The environment variable naming the orchestrator to run, as ``package.module:attr``.
ENV_VAR = "WACTORZ_ORCHESTRATOR"


def as_orchestrator(obj: Any, registry: "ActorRegistry", *, named: str = "") -> Orchestrator:
    """``obj`` as the orchestrator it is or builds, or :class:`StartupError` saying why not.

    An orchestrator may be handed over ready, or as a class or factory that is
    called with the registry, since most need it to reach the agents. Anything
    else is refused with ``named`` in the message, which is the target the
    deployment wrote, so the error points at the setting to fix.
    """
    what = named or repr(obj)
    # A class is a factory even though its unbound methods make it look like an
    # instance to the protocol check, so it is asked first.
    if not isinstance(obj, type) and isinstance(obj, Orchestrator):
        return obj
    if callable(obj):
        try:
            built = obj(registry)
        except Exception as exc:
            raise StartupError(f"orchestrator {what} could not be built: {exc}") from exc
        if isinstance(built, Orchestrator):
            return built
        raise StartupError(
            f"orchestrator {what} built {type(built).__name__}, which is not an Orchestrator "
            "(handle_turn, handle_turn_stream and commands)"
        )
    raise StartupError(
        f"orchestrator {what} is {type(obj).__name__}, which is neither an Orchestrator "
        "nor something that builds one"
    )


def resolve_orchestrator(target: str, registry: "ActorRegistry") -> Orchestrator:
    """The orchestrator ``package.module:attr`` names, built if it is a class or factory.

    Loaded the way a ``WACTORZ_AGENTS`` entry is; one that cannot be imported
    is a :class:`StartupError` naming it, since the deployment asked for it and
    answering with main instead would be a silent substitution.
    """
    try:
        obj = resolve_target(target)
    except Exception as exc:
        raise StartupError(
            f"{ENV_VAR} names {target!r}, which could not be loaded: {exc}. The module must "
            "be importable from where wactorz starts; set PYTHONPATH or install the package."
        ) from exc
    return as_orchestrator(obj, registry, named=repr(target))


__all__ = [
    "CLI",
    "DASHBOARD",
    "DIRECT_COMMANDS",
    "ENV_VAR",
    "REST",
    "SOCIAL",
    "TRUSTED_CHANNELS",
    "DirectOrchestrator",
    "MainOrchestrator",
    "Orchestrator",
    "as_orchestrator",
    "is_trusted",
    "main_commands",
    "resolve_orchestrator",
]
