"""The dashboard chat goes through the orchestrator seam.

A message that names no agent is the orchestrator's, whichever one the run
installed, and so is a slash command the orchestrator says it answers. A message
that names an agent, main included, goes to that agent as before; so does
everything when no orchestrator is installed, which is how a process that never
started one, or a test driving `route_chat` on its own, still reaches main.
"""

from typing import Any

import pytest

from wactorz.core.actor import ActorState
from wactorz.monitoring import chat_metrics
from wactorz.web import chat, runtime


class _Main:
    """Main as the registry holds it, with the stream the old path would call."""

    name = "main"
    actor_id = "main-0000"
    state = ActorState.RUNNING

    def __init__(self) -> None:
        self.streamed: list[str] = []

    async def process_user_input_stream(self, text: str, attachments: Any = None) -> Any:
        self.streamed.append(text)
        yield f"main:{text}"
        yield {"done": True, "spawned": [], "system_msg": ""}


class _Registry:
    def __init__(self, **agents: Any) -> None:
        self._agents = agents

    def find_by_name(self, name: str) -> Any:
        return self._agents.get(name)

    def all_actors(self) -> list[Any]:
        return list(self._agents.values())


class _Orchestrator:
    """Records every turn, answers in two chunks, and serves two commands."""

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    async def handle_turn(self, text: str, *, channel: str, user: str | None = None) -> str:
        raise AssertionError("the dashboard streams; it never asks for a whole answer")

    async def handle_turn_stream(
        self,
        text: str,
        *,
        channel: str,
        user: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> Any:
        self.turns.append(
            {"text": text, "channel": channel, "user": user, "attachments": attachments}
        )
        yield "an"
        yield "swer"

    def commands(self) -> frozenset[str]:
        return frozenset({"/help", "/plans"})


class _Replies:
    def __init__(self) -> None:
        self.whole: list[str] = []
        self.chunks: list[str] = []
        self.ended = 0

    async def reply(self, text: str) -> None:
        self.whole.append(text)

    async def chunk(self, text: str) -> None:
        self.chunks.append(text)

    async def end(self) -> None:
        self.ended += 1


@pytest.fixture
def main(monkeypatch: pytest.MonkeyPatch) -> _Main:
    main = _Main()
    monkeypatch.setattr(runtime, "registry", _Registry(main=main))
    return main


@pytest.fixture
def orchestrator(monkeypatch: pytest.MonkeyPatch) -> _Orchestrator:
    orchestrator = _Orchestrator()
    monkeypatch.setattr(runtime, "orchestrator", orchestrator)
    return orchestrator


@pytest.fixture
def no_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "orchestrator", None)


class TestBareText:
    async def test_reaches_the_orchestrator_and_not_main(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        replies = _Replies()

        await chat.route_chat("turn on the lights", replies.reply, replies.chunk, replies.end)

        assert orchestrator.turns == [
            {
                "text": "turn on the lights",
                "channel": "dashboard",
                "user": None,
                "attachments": None,
            }
        ]
        assert replies.chunks == ["an", "swer"]
        assert replies.ended == 1
        assert main.streamed == []

    async def test_attachments_travel_as_blocks(
        self, main: _Main, orchestrator: _Orchestrator, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocks = [{"type": "text", "text": "the file"}]
        monkeypatch.setattr(chat, "to_blocks", lambda _records, _read: blocks)
        replies = _Replies()

        await chat.route_chat(
            "read this", replies.reply, replies.chunk, replies.end, attachments=[{"name": "a.txt"}]
        )

        assert orchestrator.turns[0]["attachments"] == blocks

    async def test_is_counted_as_the_orchestrators(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        destination = chat.destination_of("hello")

        assert destination.kind == chat_metrics.ORCHESTRATOR
        assert (destination.name, destination.text) == ("main", "hello")

    async def test_without_an_orchestrator_it_is_mains_as_before(
        self, main: _Main, no_orchestrator: None
    ) -> None:
        replies = _Replies()

        await chat.route_chat("hello", replies.reply, replies.chunk, replies.end)

        assert main.streamed == ["hello"]
        assert replies.chunks == ["main:hello"]
        assert chat.destination_of("hello").kind == chat_metrics.LOCAL

    async def test_an_orchestrator_gone_mid_turn_is_said(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        destination = chat.destination_of("hello")
        runtime.set_orchestrator(None)
        replies = _Replies()

        await chat._route_chat("hello", destination, replies.reply, replies.chunk, replies.end)

        assert replies.whole == ["[error] No orchestrator is running."]
        assert replies.ended == 1


class TestNamedAgents:
    async def test_main_named_outright_is_an_agent_like_any_other(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        replies = _Replies()

        await chat.route_chat("@main hello", replies.reply, replies.chunk, replies.end)

        assert main.streamed == ["hello"]
        assert orchestrator.turns == []

    async def test_an_unknown_name_is_still_unrouted(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        replies = _Replies()

        await chat.route_chat("@ghost hello", replies.reply, replies.chunk, replies.end)

        assert replies.whole == ["Agent @ghost not found."]
        assert orchestrator.turns == []


class TestSlashCommands:
    async def test_one_the_orchestrator_serves_goes_to_it(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        replies = _Replies()

        await chat.route_chat("/plans list", replies.reply, replies.chunk, replies.end)

        assert orchestrator.turns[0]["text"] == "/plans list"
        assert replies.chunks == ["an", "swer"]
        assert replies.ended == 1

    async def test_call_syntax_still_names_the_command(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        replies = _Replies()

        await chat.route_chat("/help()", replies.reply, replies.chunk, replies.end)

        assert orchestrator.turns[0]["text"] == "/help()"

    async def test_one_it_does_not_serve_is_unknown(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        replies = _Replies()

        await chat.route_chat("/dance", replies.reply, replies.chunk, replies.end)

        assert replies.whole == ["Unknown command. Type /help for available commands."]
        assert orchestrator.turns == []
        assert main.streamed == []

    async def test_the_dashboards_own_commands_are_answered_here(
        self, main: _Main, orchestrator: _Orchestrator
    ) -> None:
        replies = _Replies()

        await chat.route_chat("/agents", replies.reply, replies.chunk, replies.end)

        assert replies.whole and replies.whole[0].startswith("Agents:")
        assert orchestrator.turns == []

    async def test_without_an_orchestrator_main_is_forwarded_to_as_before(
        self, main: _Main, no_orchestrator: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(chat, "find_main_actor", lambda _registry: main)
        replies = _Replies()

        await chat.route_chat("/plans", replies.reply, replies.chunk, replies.end)

        assert main.streamed == ["/plans"]
        assert replies.chunks == ["main:/plans"]
        assert replies.ended == 1

    def test_the_command_word(self) -> None:
        assert chat.command_word("/help") == "/help"
        assert chat.command_word("/help()") == "/help"
        assert chat.command_word("/agents delete x") == "/agents"
        assert chat.command_word("   ") == ""
