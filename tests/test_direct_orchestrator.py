"""Chat in the minimal profile: the model-free orchestrator.

No main runs there, so a message has to reach an agent on its own. ``@name
{json}`` is a task for that agent and the reply is its return value; bare text
has no model to read it and is answered with the agents and how to address one;
the commands that need only the registry work, and nothing else does. The last
tests drive the dashboard route itself, over a real registry with a real
function agent, which is the turn a person types in the minimal profile.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wactorz.agents.function_agent import agent, spec_of
from wactorz.core.actor import Actor, ActorState, ReplyError
from wactorz.core.registry import ActorRegistry
from wactorz.orchestration import DIRECT_COMMANDS, DirectOrchestrator, Orchestrator
from wactorz.web import chat, runtime


class _Spec:
    def __init__(self, subscribes: tuple[str, ...] = (), publishes: str = "") -> None:
        self.subscribes = subscribes
        self.publishes = publishes


def _actor(name: str, state: ActorState = ActorState.RUNNING, spec: _Spec | None = None) -> Any:
    return SimpleNamespace(name=name, actor_id=f"{name}-0123456789", state=state, spec=spec)


class _Registry:
    """The three registry calls the orchestrator makes, with `ask` scripted."""

    def __init__(self, *actors: Any) -> None:
        self._actors = list(actors)
        self.asked: list[tuple[str, Any, float]] = []
        self.answer: Any = {"score": 12.4}
        self.raises: BaseException | None = None

    def all_actors(self) -> list[Any]:
        return self._actors

    def find_by_name(self, name: str) -> Any:
        return next((a for a in self._actors if a.name == name), None)

    async def ask(self, target: str, payload: Any, *, timeout: float) -> Any:
        self.asked.append((target, payload, timeout))
        if self.raises is not None:
            raise self.raises
        return self.answer


def _direct(*actors: Any, **kwargs: Any) -> tuple[DirectOrchestrator, _Registry]:
    registry = _Registry(*actors)
    # The orchestrator asks and lists through the registry; the fake offers both.
    return DirectOrchestrator(cast(ActorRegistry, registry), **kwargs), registry


class TestAskingAnAgent:
    async def test_json_after_the_name_is_the_task_and_the_reply_is_shown(self) -> None:
        orchestrator, registry = _direct(_actor("imu-anomaly"))

        answer = await orchestrator.handle_turn(
            '@imu-anomaly {"ax": 9, "ay": -7.5, "az": 1}', channel="dashboard"
        )

        assert registry.asked == [("imu-anomaly", {"ax": 9, "ay": -7.5, "az": 1}, 150.0)]
        assert answer == '{"score": 12.4}'

    async def test_words_after_the_name_travel_as_text(self) -> None:
        orchestrator, registry = _direct(_actor("greeter"))
        registry.answer = {"result": "hello back"}

        answer = await orchestrator.handle_turn("@greeter hello there", channel="cli")

        assert registry.asked[0][1] == {"text": "hello there"}
        assert answer == "hello back"

    async def test_a_name_alone_shows_usage(self) -> None:
        orchestrator, registry = _direct(_actor("greeter"))

        answer = await orchestrator.handle_turn("@greeter", channel="cli")

        assert answer.startswith("[usage] @greeter")
        assert registry.asked == []

    async def test_an_unknown_agent_is_said_with_the_running_ones(self) -> None:
        orchestrator, registry = _direct(_actor("imu-anomaly"), _actor("monitor"))
        registry.raises = LookupError("no agent named 'ghost' is running")

        answer = await orchestrator.handle_turn("@ghost hi", channel="dashboard")

        assert answer == "No agent called @ghost is running.\nRunning: @imu-anomaly, @monitor"

    async def test_an_error_reply_is_the_agents_words(self) -> None:
        orchestrator, registry = _direct(_actor("imu-anomaly"))
        registry.raises = ReplyError("imu-anomaly", {"error": "ax is not a number"})

        answer = await orchestrator.handle_turn("@imu-anomaly {}", channel="dashboard")

        assert (
            answer
            == "[error] @imu-anomaly: 'imu-anomaly' answered with an error: ax is not a number"
        )

    async def test_a_silent_agent_is_given_up_on_and_said(self) -> None:
        orchestrator, registry = _direct(_actor("slow"), timeout=2.5)
        registry.raises = asyncio.TimeoutError()

        answer = await orchestrator.handle_turn("@slow think", channel="dashboard")

        assert registry.asked[0][2] == 2.5
        assert answer == "[error] @slow did not reply within 2.5s."


class TestBareText:
    async def test_lists_the_agents_and_how_to_address_one(self) -> None:
        orchestrator, registry = _direct(_actor("imu-anomaly"), _actor("monitor"))

        answer = await orchestrator.handle_turn("turn on the lights", channel="dashboard")

        assert "No model runs in this profile" in answer
        assert "Running: @imu-anomaly, @monitor" in answer
        assert "@<agent>" in answer
        assert registry.asked == []

    async def test_with_nothing_running_it_says_so(self) -> None:
        orchestrator, _ = _direct()

        assert "Running: (none)" in await orchestrator.handle_turn("hello", channel="rest")


class TestCommands:
    def test_it_serves_the_registry_commands_only(self) -> None:
        orchestrator, _ = _direct()

        assert (
            orchestrator.commands() == DIRECT_COMMANDS == {"/agents", "/topics", "/nodes", "/help"}
        )

    async def test_agents_lists_state_and_name(self) -> None:
        orchestrator, _ = _direct(_actor("imu-anomaly"), _actor("monitor", ActorState.STOPPED))

        answer = await orchestrator.handle_turn("/agents", channel="dashboard")

        assert answer.splitlines()[0] == "Agents:"
        assert "[running ] @imu-anomaly" in answer
        assert "[stopped ] @monitor" in answer

    async def test_agents_with_nothing_running(self) -> None:
        orchestrator, _ = _direct()

        assert (
            await orchestrator.handle_turn("/agents", channel="dashboard") == "No agents running."
        )

    async def test_nodes_is_this_process(self) -> None:
        orchestrator, _ = _direct(_actor("imu-anomaly"))

        answer = await orchestrator.handle_turn("/nodes", channel="dashboard")

        assert answer == "Nodes:\n  local    online   @imu-anomaly"

    async def test_topics_come_from_what_the_agents_declare(self) -> None:
        class Bridge:
            SUBSCRIBES = ("ha/state",)
            PUBLISHES = "bridge/out"
            name = "bridge"
            actor_id = "bridge-0123456789"
            state = ActorState.RUNNING

        orchestrator, _ = _direct(
            _actor("imu-anomaly", spec=_Spec(("imu/raw",), "anomalies/imu")),
            _actor("plain"),
            Bridge(),
        )

        answer = await orchestrator.handle_turn("/topics", channel="dashboard")

        lines = [" ".join(line.split()) for line in answer.splitlines()]
        assert lines[0].startswith("Topics")
        assert "anomalies/imu ← @imu-anomaly" in lines
        assert "imu/raw → @imu-anomaly" in lines
        assert "bridge/out ← @bridge" in lines
        assert "ha/state → @bridge" in lines
        assert not any("@plain" in line for line in lines)

    async def test_topics_with_none_declared(self) -> None:
        orchestrator, _ = _direct(_actor("plain"))

        answer = await orchestrator.handle_turn("/topics", channel="dashboard")

        assert answer == "No topics declared by the running agents."

    async def test_help_names_the_commands_and_the_agents(self) -> None:
        orchestrator, _ = _direct(_actor("imu-anomaly"))

        answer = await orchestrator.handle_turn("/help()", channel="dashboard")

        assert "@<agent> {json}" in answer
        assert all(command in answer for command in DIRECT_COMMANDS)
        assert answer.endswith("Running: @imu-anomaly")

    async def test_anything_else_is_unknown(self) -> None:
        orchestrator, registry = _direct(_actor("imu-anomaly"))

        answer = await orchestrator.handle_turn("/plans", channel="dashboard")

        assert answer == "Unknown command. Type /help for available commands."
        assert registry.asked == []


class TestStreaming:
    async def test_the_answer_comes_in_one_piece(self) -> None:
        orchestrator, _ = _direct(_actor("imu-anomaly"))

        chunks = [
            c async for c in orchestrator.handle_turn_stream("@imu-anomaly {}", channel="cli")
        ]

        assert chunks == ['{"score": 12.4}']

    async def test_attachments_are_said_to_be_left_behind(self) -> None:
        orchestrator, _ = _direct(_actor("imu-anomaly"))

        chunks = [
            c
            async for c in orchestrator.handle_turn_stream(
                "/nodes", channel="dashboard", attachments=[{"type": "text", "text": "a file"}]
            )
        ]

        assert chunks[0].startswith("[note] attachments not sent")
        assert chunks[1].startswith("Nodes:")

    def test_it_is_an_orchestrator(self) -> None:
        orchestrator, _ = _direct()

        assert isinstance(orchestrator, Orchestrator)


# ── The dashboard route, over a real registry and a real function agent ──────


@agent(name="imu-anomaly", description="scores a reading")
def score(reading: dict) -> dict:
    return {"score": abs(reading["ax"]) + abs(reading["ay"]), "reading": reading}


class _Live:
    """A registry whose agents' message loops run: what delivers a task and its reply."""

    def __init__(self) -> None:
        self.registry = ActorRegistry()
        self._loops: list[asyncio.Task[Any]] = []

    async def add(self, fn: Any, state_dir: Path) -> Actor:
        spec = spec_of(fn)
        assert spec is not None
        actor = spec.build(persistence_dir=str(state_dir))
        actor.state = ActorState.RUNNING
        await self.registry.register(actor)
        self._loops.append(asyncio.create_task(actor._message_loop()))
        return actor

    async def close(self) -> None:
        for task in self._loops:
            task.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)


@pytest.fixture(name="minimal")
async def minimal_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Live]:
    """The minimal profile as the dashboard sees it: a registry, and the model-free orchestrator."""
    live = _Live()
    await live.add(score, tmp_path)
    monkeypatch.setattr(runtime, "registry", live.registry)
    monkeypatch.setattr(runtime, "orchestrator", DirectOrchestrator(live.registry))
    yield live
    await live.close()


class _Replies:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.ended = 0

    async def reply(self, text: str) -> None:
        self.said.append(text)

    async def end(self) -> None:
        self.ended += 1


class TestTheDashboardInTheMinimalProfile:
    async def test_the_detector_answers_with_its_score(self, minimal: _Live) -> None:
        replies = _Replies()

        await chat.route_chat(
            '@imu-anomaly {"ax": 9, "ay": -7.5, "az": 1}', replies.reply, replies.reply, replies.end
        )

        assert replies.said == ['{"score": 16.5, "reading": {"ax": 9, "ay": -7.5, "az": 1}}']
        assert replies.ended == 1

    async def test_bare_text_lists_the_agents(self, minimal: _Live) -> None:
        replies = _Replies()

        await chat.route_chat("what is the weather", replies.reply, replies.reply, replies.end)

        (answer,) = replies.said
        assert "No model runs in this profile" in answer
        assert "Running: @imu-anomaly" in answer
        assert replies.ended == 1

    async def test_help_reaches_the_orchestrator(self, minimal: _Live) -> None:
        replies = _Replies()

        await chat.route_chat("/help", replies.reply, replies.reply, replies.end)

        (answer,) = replies.said
        assert "@<agent> {json}" in answer
