"""The interactive CLI: what each command does, and what it prints.

`run()` is one long dispatch loop, and almost all of it was unreachable from a
test because it blocks on stdin. It is driven here by replacing `_prompt` with a
scripted queue — the loop then runs exactly as it does for a person typing, and
ends on the `quit` the script finishes with.

Printed output *is* this interface's return value, so the assertions read it back
from `capsys` rather than inspecting state. That is also why the module carries
the suite's `print` findings: they are the product, not debug leftovers.

Nothing here needs an optional extra. The CLI imports only stdlib and wactorz's
own modules, so these run on a checkout installed with `[dev]` alone.
"""

import asyncio
from typing import Any

import pytest

from wactorz.interfaces.chat.cli import CLIInterface


class _Registry:
    def __init__(self, agents: dict[str, Any] | None = None) -> None:
        self._agents = agents or {}

    def find_by_name(self, name: str) -> Any:
        return self._agents.get(name)


class _StreamingAgent:
    """An agent with `chat_stream`, which the CLI streams rather than awaits."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def chat_stream(self, _message: str) -> Any:
        for chunk in self.chunks:
            yield chunk


class _PlainAgent:
    """An agent without `chat_stream`; the CLI asks for a whole answer instead."""


class _MainActor:
    """Only the surface `run()` actually touches."""

    def __init__(self) -> None:
        self._registry: _Registry | None = _Registry()
        self.persisted: dict[str, Any] = {}
        self.agents: list[dict[str, Any]] = []
        self.nodes: list[dict[str, Any]] = []
        self.stream_chunks: list[Any] = ["hel", "lo"]
        self.migrate_result: dict[str, Any] = {"success": True, "message": "moved"}
        self.installer_result: dict[str, Any] = {"success": True}
        self.installer_calls: list[dict[str, Any]] = []

    def persist(self, key: str, value: Any) -> None:
        self.persisted[key] = value

    async def list_agents(self) -> list[dict[str, Any]]:
        return self.agents

    def list_nodes(self) -> list[dict[str, Any]]:
        return self.nodes

    async def migrate_agent(self, _name: str, _node: str) -> dict[str, Any]:
        return self.migrate_result

    async def delegate_to_installer(self, payload: dict[str, Any], timeout: float) -> Any:
        self.installer_calls.append({**payload, "timeout": timeout})
        return self.installer_result

    async def process_user_input_stream(self, _text: str) -> Any:
        for chunk in self.stream_chunks:
            yield chunk


@pytest.fixture(name="actor")
def actor_fixture() -> _MainActor:
    return _MainActor()


@pytest.fixture(name="drive")
def drive_fixture(monkeypatch: pytest.MonkeyPatch):
    """Run the CLI over a scripted list of inputs and return what it printed."""

    async def _drive(interface: CLIInterface, lines: list[str], capsys: Any) -> str:
        queued = [*lines, "quit"]

        async def _fake_prompt(_prompt: str) -> str:
            if not queued:
                raise EOFError
            return queued.pop(0)

        monkeypatch.setattr(CLIInterface, "_prompt", staticmethod(_fake_prompt))
        await interface.run()
        return capsys.readouterr().out

    return _drive


class TestLeaving:
    @pytest.mark.parametrize("word", ["quit", "exit", "QUIT"])
    async def test_it_says_goodbye_and_stops(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str], word: str
    ) -> None:
        out = await drive(CLIInterface(actor), [word], capsys)  # type: ignore[arg-type]

        assert "Goodbye" in out

    async def test_an_interrupt_at_the_prompt_leaves_cleanly(
        self, actor: _MainActor, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ctrl-C at the prompt is how people leave, so it is an exit rather
        than a traceback."""

        async def _interrupt(_prompt: str) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(CLIInterface, "_prompt", staticmethod(_interrupt))

        await CLIInterface(actor).run()  # type: ignore[arg-type]

        assert "Goodbye" in capsys.readouterr().out

    async def test_a_blank_line_is_skipped(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["", "   "], capsys)  # type: ignore[arg-type]

        assert "@main" not in out


class TestInformationCommands:
    async def test_help_lists_the_commands(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["/help"], capsys)  # type: ignore[arg-type]

        assert "/agents" in out
        assert "/deploy" in out

    async def test_clearing_the_plan_cache_persists_an_empty_one(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["/clear-plans"], capsys)  # type: ignore[arg-type]

        assert actor.persisted["_plan_cache"] == {}
        assert "cleared" in out.lower()

    async def test_agents_shows_state_name_and_flags(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        actor.agents = [
            {"state": "running", "name": "main", "actor_id": "abcdef1234", "protected": True},
            {"state": "stopped", "name": "picam", "actor_id": "99887766", "node": "rpi"},
        ]

        out = await drive(CLIInterface(actor), ["/agents"], capsys)  # type: ignore[arg-type]

        assert "@main" in out
        assert "[protected]" in out
        assert "[rpi]" in out

    async def test_nodes_says_so_when_none_have_been_seen(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty list and a broken heartbeat look identical otherwise."""
        actor.agents = [{"state": "running", "name": "main", "actor_id": "abcdef12"}]

        out = await drive(CLIInterface(actor), ["/nodes"], capsys)  # type: ignore[arg-type]

        assert "no remote nodes" in out
        assert "@main" in out

    async def test_nodes_marks_an_offline_node(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        actor.agents = []
        actor.nodes = [
            {"node": "rpi-kitchen", "online": False, "agents": ["temp"]},
            {"node": "rpi-shed", "online": True, "agents": []},
        ]

        out = await drive(CLIInterface(actor), ["/nodes"], capsys)  # type: ignore[arg-type]

        assert "OFFLINE" in out
        assert "(no agents)" in out


class TestMigrate:
    async def test_it_explains_itself_when_given_too_little(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["/migrate onlyone"], capsys)  # type: ignore[arg-type]

        assert "[usage] /migrate" in out

    async def test_it_reports_the_result(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["/migrate temp rpi"], capsys)  # type: ignore[arg-type]

        assert "[OK] moved" in out

    async def test_a_failed_migration_says_fail(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        actor.migrate_result = {"success": False, "message": "no such node"}

        out = await drive(CLIInterface(actor), ["/migrate temp nope"], capsys)  # type: ignore[arg-type]

        assert "[FAIL] no such node" in out


class TestDeployPkg:
    async def test_it_explains_itself_when_given_too_little(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["/deploy-pkg rpi"], capsys)  # type: ignore[arg-type]

        assert "[usage] /deploy-pkg" in out

    async def test_an_unconfigured_target_is_refused_with_help(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Credentials come from the environment, so an unknown name has nowhere
        to send the install — and the message names the variables to set."""
        out = await drive(CLIInterface(actor), ["/deploy-pkg nowhere numpy"], capsys)  # type: ignore[arg-type]

        assert "[error]" in out
        assert not actor.installer_calls


class TestDeploy:
    async def test_no_argument_lists_the_configured_targets(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["/deploy"], capsys)  # type: ignore[arg-type]

        assert "[usage] /deploy" in out

    async def test_a_host_override_is_refused(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The old form took a host here, which aimed one target's credentials at
        a machine of the caller's choosing. Targets are whole or unused."""
        out = await drive(CLIInterface(actor), ["/deploy rpi 10.0.0.9"], capsys)  # type: ignore[arg-type]

        assert "node name only" in out

    async def test_a_named_target_reaches_the_deploy_path(
        self,
        actor: _MainActor,
        drive: Any,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        seen: list[str] = []

        async def _fake_deploy(_self: CLIInterface, node: str) -> None:
            seen.append(node)

        monkeypatch.setattr(CLIInterface, "_deploy", _fake_deploy)

        await drive(CLIInterface(actor), ["/deploy rpi-kitchen"], capsys)  # type: ignore[arg-type]

        assert seen == ["rpi-kitchen"]


class TestAddressingAnAgent:
    async def test_a_mention_with_no_message_shows_usage(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["@picam"], capsys)  # type: ignore[arg-type]

        assert "[usage] @picam" in out

    async def test_addressing_main_goes_through_the_orchestrator(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`@main` is the full pipeline, not a direct send — the CLI recognises
        its own actor and streams the orchestrated turn."""
        interface = CLIInterface(actor)  # type: ignore[arg-type]
        actor._registry = _Registry({"main": actor})

        out = await drive(interface, ["@main hello there"], capsys)

        assert "routing to @main" in out
        assert "hello" in out

    async def test_a_streaming_agent_is_streamed(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        actor._registry = _Registry({"chatty": _StreamingAgent(["one ", "two"])})

        out = await drive(CLIInterface(actor), ["@chatty hi"], capsys)  # type: ignore[arg-type]

        assert "one two" in out

    async def test_a_plain_agent_is_asked_for_a_whole_answer(
        self,
        actor: _MainActor,
        drive: Any,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        actor._registry = _Registry({"quiet": _PlainAgent()})

        async def _answer(_self: CLIInterface, name: str, message: str) -> str:
            return f"{name} says {message}"

        monkeypatch.setattr(CLIInterface, "_get_agent_response", _answer)

        out = await drive(CLIInterface(actor), ["@quiet ping"], capsys)  # type: ignore[arg-type]

        assert "quiet says ping" in out

    async def test_an_unknown_agent_is_tried_on_the_remote_nodes(
        self,
        actor: _MainActor,
        drive: Any,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Absent from the local registry does not mean absent — it may live on a
        node, which is reachable only over the broker."""

        async def _remote(_self: CLIInterface, name: str, _message: str) -> str:
            return f"remote {name}"

        monkeypatch.setattr(CLIInterface, "_get_remote_agent_response", _remote)

        out = await drive(CLIInterface(actor), ["@faraway ping"], capsys)  # type: ignore[arg-type]

        assert "remote faraway" in out


class TestPlainText:
    async def test_it_streams_through_main(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = await drive(CLIInterface(actor), ["what is the weather"], capsys)  # type: ignore[arg-type]

        assert "@main:" in out
        assert "hello" in out

    async def test_a_system_message_is_shown_after_the_answer(
        self, actor: _MainActor, drive: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The dict chunk is out-of-band — it is the system talking about the
        turn, not part of it, so it must not land inside the streamed text."""
        actor.stream_chunks = ["done", {"system_msg": "spawned @weather"}]

        out = await drive(CLIInterface(actor), ["make me a weather agent"], capsys)  # type: ignore[arg-type]

        assert "[System: spawned @weather]" in out
        assert "done" in out


class TestReadingAPrompt:
    async def test_it_reads_a_line_off_a_daemon_thread(self) -> None:
        """The read runs on a daemon thread so a parked `input()` cannot delay
        interpreter exit — the reason stopping the CLI used to need several
        Ctrl-Cs.
        """
        import builtins

        original = builtins.input
        builtins.input = lambda _prompt="": "typed"
        try:
            assert await CLIInterface._prompt("You: ") == "typed"
        finally:
            builtins.input = original

    async def test_an_eof_cancels_rather_than_returning_a_value(self) -> None:
        import builtins

        def _eof(_prompt: str = "") -> str:
            raise EOFError

        original = builtins.input
        builtins.input = _eof
        try:
            with pytest.raises(asyncio.CancelledError):
                await CLIInterface._prompt("You: ")
        finally:
            builtins.input = original
