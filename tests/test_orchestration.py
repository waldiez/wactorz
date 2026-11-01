"""The orchestrator seam, with main behind it.

Every chat surface will call an :class:`Orchestrator` rather than main by name,
so what matters here is that main's adapter forwards each channel to the entry
point that channel is allowed to reach, carries a stream and its attachments
through unchanged, labels main's work as main's, and finds main again after a
restart has replaced the instance.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from wactorz import app as app_module
from wactorz import orchestration
from wactorz.core.actor import ActorState
from wactorz.core.registry import ActorRegistry
from wactorz.core.turns import current_agent
from wactorz.orchestration import DirectOrchestrator, MainOrchestrator, Orchestrator
from wactorz.web import runtime


class _Main:
    """Main's three chat entry points, recording what reached each."""

    name = "main"
    #: Unset on a plain fake: an actor's state is read only when it has one.
    state: ActorState | None = None

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.chunks: list[Any] = ["hel", "lo", {"done": True, "spawned": [], "system_msg": ""}]
        #: The agent label seen inside the stream, once per chunk produced.
        self.labels: list[str] = []

    async def process_user_input(self, text: str) -> str:
        self.calls.append(("full", text))
        return f"full:{text}"

    async def process_user_input_restricted(self, text: str) -> str:
        self.calls.append(("restricted", text))
        return f"restricted:{text}"

    async def process_user_input_stream(
        self, text: str, attachments: list[dict[str, Any]] | None = None
    ) -> Any:
        self.calls.append(("stream", text, attachments))
        for chunk in self.chunks:
            self.labels.append(current_agent())
            yield chunk


class _Registry:
    """Only `find_by_name`, with whatever is under main's name swappable."""

    def __init__(self, main: Any = None) -> None:
        self.main = main

    def find_by_name(self, name: str) -> Any:
        return self.main if name == "main" else None


def _orchestrator(main: Any) -> tuple[MainOrchestrator, _Registry]:
    registry = _Registry(main)
    # The adapter only looks a name up, which is all the fake offers.
    return MainOrchestrator(cast(ActorRegistry, registry)), registry


class TestHandleTurn:
    @pytest.mark.parametrize(
        "channel", [orchestration.DASHBOARD, orchestration.CLI, orchestration.REST]
    )
    async def test_a_trusted_channel_reaches_the_full_entry(self, channel: str) -> None:
        main = _Main()
        orchestrator, _ = _orchestrator(main)

        answer = await orchestrator.handle_turn("hello", channel=channel)

        assert answer == "full:hello"
        assert main.calls == [("full", "hello")]

    @pytest.mark.parametrize("channel", [orchestration.SOCIAL, "slack", ""])
    async def test_any_other_channel_reaches_the_restricted_entry(self, channel: str) -> None:
        main = _Main()
        orchestrator, _ = _orchestrator(main)

        answer = await orchestrator.handle_turn("hello", channel=channel, user="42")

        assert answer == "restricted:hello"
        assert main.calls == [("restricted", "hello")]

    async def test_main_is_looked_up_on_every_turn(self) -> None:
        first, second = _Main(), _Main()
        orchestrator, registry = _orchestrator(first)

        await orchestrator.handle_turn("one", channel=orchestration.CLI)
        registry.main = second
        await orchestrator.handle_turn("two", channel=orchestration.CLI)

        assert first.calls == [("full", "one")]
        assert second.calls == [("full", "two")]

    async def test_without_main_the_turn_fails_naming_it(self) -> None:
        orchestrator, _ = _orchestrator(None)

        with pytest.raises(LookupError, match="main"):
            await orchestrator.handle_turn("hello", channel=orchestration.CLI)

    @pytest.mark.parametrize(
        ("state", "said"),
        [(ActorState.FAILED, "has failed"), (ActorState.STOPPED, "is stopped")],
    )
    async def test_a_main_that_cannot_answer_is_not_asked(
        self, state: ActorState, said: str
    ) -> None:
        main = _Main()
        main.state = state
        orchestrator, _ = _orchestrator(main)

        with pytest.raises(LookupError, match=f"main {said}"):
            await orchestrator.handle_turn("hello", channel=orchestration.CLI)

        assert main.calls == []

    async def test_a_running_main_answers(self) -> None:
        main = _Main()
        main.state = ActorState.RUNNING
        orchestrator, _ = _orchestrator(main)

        assert await orchestrator.handle_turn("hello", channel=orchestration.CLI) == "full:hello"


class TestHandleTurnStream:
    async def test_words_come_through_and_the_summary_dict_does_not(self) -> None:
        main = _Main()
        orchestrator, _ = _orchestrator(main)
        blocks = [{"type": "text", "text": "a file"}]

        chunks = [
            chunk
            async for chunk in orchestrator.handle_turn_stream(
                "hello", channel=orchestration.DASHBOARD, attachments=blocks
            )
        ]

        assert chunks == ["hel", "lo"]
        assert main.calls == [("stream", "hello", blocks)]

    async def test_mains_work_is_labelled_as_mains_and_the_label_does_not_leak(self) -> None:
        main = _Main()
        orchestrator, _ = _orchestrator(main)
        between: list[str] = []

        async for _chunk in orchestrator.handle_turn_stream("hello", channel=orchestration.CLI):
            between.append(current_agent())

        assert main.labels == ["main", "main", "main"]
        assert between == ["", ""]
        assert current_agent() == ""

    async def test_a_consumer_that_stops_early_leaves_no_label_behind(self) -> None:
        main = _Main()
        orchestrator, _ = _orchestrator(main)

        async for _chunk in orchestrator.handle_turn_stream("hello", channel=orchestration.CLI):
            break

        assert current_agent() == ""

    async def test_a_social_channel_gets_the_restricted_answer_in_one_piece(self) -> None:
        main = _Main()
        orchestrator, _ = _orchestrator(main)

        chunks = [
            chunk
            async for chunk in orchestrator.handle_turn_stream(
                "hello", channel=orchestration.SOCIAL, user="42"
            )
        ]

        assert chunks == ["restricted:hello"]
        assert main.calls == [("restricted", "hello")]

    async def test_without_main_the_stream_fails_naming_it(self) -> None:
        orchestrator, _ = _orchestrator(None)

        with pytest.raises(LookupError, match="main"):
            async for _chunk in orchestrator.handle_turn_stream("hi", channel=orchestration.CLI):
                pass


class TestCommands:
    def test_mains_commands_are_its_slash_commands_by_first_word(self) -> None:
        orchestrator, _ = _orchestrator(_Main())

        commands = orchestrator.commands()

        assert {"/help", "/agents", "/nodes", "/topics", "/migrate", "/deploy"} <= commands
        # The short forms the dispatcher rewrites are commands a person types too.
        assert {"/delete", "/stop"} <= commands
        assert all(name.startswith("/") and " " not in name for name in commands)
        assert "help" not in commands

    def test_the_adapter_is_an_orchestrator(self) -> None:
        orchestrator, _ = _orchestrator(_Main())

        assert isinstance(orchestrator, Orchestrator)

    def test_trust_is_by_name(self) -> None:
        assert orchestration.is_trusted(orchestration.DASHBOARD)
        assert not orchestration.is_trusted(orchestration.SOCIAL)
        assert not orchestration.is_trusted("anything-else")


class TestInstalling:
    @pytest.fixture(autouse=True)
    def _no_orchestrator(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runtime, "orchestrator", None)

    def test_the_runtime_slot_is_set_and_cleared(self) -> None:
        orchestrator, _ = _orchestrator(_Main())

        runtime.set_orchestrator(orchestrator)
        assert runtime.orchestrator is orchestrator

        runtime.set_orchestrator(None)
        assert runtime.orchestrator is None

    async def test_startup_installs_mains_adapter_when_main_runs(self) -> None:
        main = _Main()
        system = SimpleNamespace(registry=_Registry(main))

        chosen = app_module.install_orchestrator(cast(Any, system), main)

        assert isinstance(chosen, MainOrchestrator)
        assert runtime.orchestrator is chosen
        assert await chosen.handle_turn("hello", channel=orchestration.DASHBOARD) == "full:hello"

    def test_startup_installs_the_model_free_one_without_main(self) -> None:
        system = SimpleNamespace(registry=_Registry(None))

        chosen = app_module.install_orchestrator(cast(Any, system), None)

        assert isinstance(chosen, DirectOrchestrator)
        assert runtime.orchestrator is chosen
