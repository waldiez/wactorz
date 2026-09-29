"""How a message on the wire becomes a command the node carries out.

The handlers are covered elsewhere by calling them. What is covered here is the
seam those tests skip: the connection that holds the node's control topics open,
and the table that decides which handler a topic belongs to. A message that
reaches the broker and no handler is the failure this exists to catch, and it
looks like nothing at all from the outside.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from wactorz.core import mqtt as core_mqtt
from wactorz.node import runner as runner_mod
from wactorz.node.runner import NodeRunner

CONTROL_HANDLERS = [
    ("desired_state", "_on_desired_state"),
    ("spawn", "_on_spawn"),
    ("stop", "_on_stop"),
    ("migrate", "_on_migrate"),
    ("stop_all", "_on_stop_all"),
    ("restart", "_on_restart"),
    ("restart_agent", "_on_restart_agent"),
    ("list", "_on_list"),
    ("code_request", "_on_code_request"),
]


@pytest.fixture(name="runner")
def runner_fixture(tmp_path: Path) -> NodeRunner:
    runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))

    async def _publish(topic: str, data: Any, retain: bool = False, **_kw: Any) -> None:
        return None

    runner.publish = _publish  # type: ignore[method-assign]
    return runner


def _record(runner: NodeRunner, name: str, seen: list[str]) -> None:
    async def _handler(topic: str, data: Any, msg: Any) -> None:
        seen.append(name)

    setattr(runner, name, _handler)


def _all_recorded(runner: NodeRunner, seen: list[str]) -> None:
    for _leaf, handler in CONTROL_HANDLERS:
        _record(runner, handler, seen)
    _record(runner, "_on_reply", seen)
    _record(runner, "_on_task", seen)


class _Message:
    def __init__(self, topic: str, payload: bytes = b"{}") -> None:
        self.topic = topic
        self.payload = payload
        self.properties = None


class TestEveryControlTopicReachesItsHandler:
    @pytest.mark.parametrize(("leaf", "handler"), CONTROL_HANDLERS)
    async def test_it_routes(self, runner: NodeRunner, leaf: str, handler: str) -> None:
        seen: list[str] = []
        _all_recorded(runner, seen)
        topic = f"nodes/rpi/{leaf}"

        await runner._dispatch_control(topic, {}, _Message(topic))

        assert seen == [handler]

    async def test_a_reply_goes_to_the_agent_waiting_on_it(self, runner: NodeRunner) -> None:
        seen: list[str] = []
        _all_recorded(runner, seen)
        topic = "nodes/rpi/reply/abc123"

        await runner._dispatch_control(topic, {}, _Message(topic))

        assert seen == ["_on_reply"]

    async def test_a_task_addressed_by_name_goes_to_the_task_handler(
        self, runner: NodeRunner
    ) -> None:
        seen: list[str] = []
        _all_recorded(runner, seen)
        topic = "agents/by-name/collector/task"

        await runner._dispatch_control(topic, {}, _Message(topic))

        assert seen == ["_on_task"]

    async def test_another_nodes_topic_is_not_ours_to_act_on(self, runner: NodeRunner) -> None:
        seen: list[str] = []
        _all_recorded(runner, seen)
        topic = "nodes/some-other-node/stop_all"

        await runner._dispatch_control(topic, {}, _Message(topic))

        assert seen == []

    async def test_a_topic_no_handler_claims_is_ignored(self, runner: NodeRunner) -> None:
        seen: list[str] = []
        _all_recorded(runner, seen)

        await runner._dispatch_control("nodes/rpi/weather", {}, _Message("nodes/rpi/weather"))

        assert seen == []

    def test_the_table_and_the_subscriptions_agree(self, runner: NodeRunner) -> None:
        """A handler for a topic the node never subscribes to is never reached.

        The two lists are written separately, and nothing else would notice one
        gaining an entry the other does not have.
        """
        source = Path("wactorz/node/runner.py").read_text(encoding="utf-8")
        for leaf, _handler in CONTROL_HANDLERS:
            assert f'f"nodes/{{self.node_name}}/{leaf}"' in source, leaf


class _Client:
    def __init__(self, messages: Iterable[_Message], drained: asyncio.Event) -> None:
        self._messages = list(messages)
        self._drained = drained
        self.subscribed: list[str] = []

    async def subscribe(self, topic: str, **_kw: Any) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> AsyncIterator[_Message]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[_Message]:
        for message in self._messages:
            yield message
        self._drained.set()
        await asyncio.Event().wait()  # stay connected, as a broker would


class _Broker:
    def __init__(self, messages: Iterable[_Message]) -> None:
        self.drained = asyncio.Event()
        self.client = _Client(messages, self.drained)

    def __call__(self, host: str, port: int, **kwargs: Any) -> "_Broker":
        self.kwargs = kwargs
        return self

    async def __aenter__(self) -> _Client:
        return self.client

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Broker]:
    def _build(*messages: _Message) -> _Broker:
        fake = _Broker(messages)
        monkeypatch.setattr(core_mqtt, "mqtt_client", fake)
        monkeypatch.setattr(runner_mod, "mqtt_client", fake)
        return fake

    return _build


async def _drive(runner: NodeRunner, broker: _Broker) -> None:
    """Run the subscriber until the broker has handed over everything."""
    runner._running = True
    task = asyncio.create_task(runner._subscriber_loop())
    try:
        await asyncio.wait_for(broker.drained.wait(), timeout=2)
        await asyncio.sleep(0)
    finally:
        runner._running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestTheControlConnection:
    async def test_it_holds_every_topic_the_node_answers_on(
        self, runner: NodeRunner, broker: Any
    ) -> None:
        fake = broker()

        await _drive(runner, fake)

        assert set(fake.client.subscribed) == {
            "nodes/rpi/spawn",
            "nodes/rpi/desired_state",
            "nodes/rpi/stop",
            "nodes/rpi/stop_all",
            "nodes/rpi/restart",
            "nodes/rpi/restart_agent",
            "nodes/rpi/migrate",
            "nodes/rpi/list",
            "nodes/rpi/code_request",
            "nodes/rpi/reply/#",
            "agents/by-name/+/task",
        }

    async def test_a_message_reaches_its_handler_decoded(
        self, runner: NodeRunner, broker: Any
    ) -> None:
        got: list[Any] = []

        async def _on_stop(topic: str, data: Any, msg: Any) -> None:
            got.append(data)

        runner._on_stop = _on_stop  # type: ignore[method-assign]
        fake = broker(_Message("nodes/rpi/stop", json.dumps({"name": "collector"}).encode()))

        await _drive(runner, fake)

        assert got == [{"name": "collector"}]

    async def test_a_payload_that_is_not_json_still_reaches_it(
        self, runner: NodeRunner, broker: Any
    ) -> None:
        # The legacy bare-name stop is exactly this, and dropping it would make
        # an older main's command vanish with nothing said.
        got: list[Any] = []

        async def _on_stop(topic: str, data: Any, msg: Any) -> None:
            got.append(data)

        runner._on_stop = _on_stop  # type: ignore[method-assign]
        fake = broker(_Message("nodes/rpi/stop", b"collector"))

        await _drive(runner, fake)

        assert got == ["collector"]

    async def test_a_refused_message_never_reaches_a_handler(
        self, runner: NodeRunner, broker: Any
    ) -> None:
        # What the signing guard is for: refused here, before any handler runs.
        got: list[Any] = []

        async def _on_spawn(topic: str, data: Any, msg: Any) -> None:
            got.append(data)

        runner._on_spawn = _on_spawn  # type: ignore[method-assign]
        runner._admit_control = lambda _topic, _msg: False  # type: ignore[method-assign]
        fake = broker(_Message("nodes/rpi/spawn", b'{"name": "x"}'))

        await _drive(runner, fake)

        assert got == []


class TestStartingTheProcess:
    """`wactorz --node` — what happens between the flag and the runner.

    Untested until now, and it is the only path a real node ever takes: the
    checks that refuse a node that could never work, the signal handlers that
    let it be stopped, and closing the loop on the way out.
    """

    @staticmethod
    def _args(**over: Any) -> Any:
        from argparse import Namespace

        base = {
            "node": "rpi",
            "name": None,
            "broker": None,
            "port": None,
            "mqtt_broker": "broker.lan",
            "mqtt_port": 8883,
            "loglevel": "INFO",
        }
        return Namespace(**{**base, **over})

    def _runner(self, monkeypatch: pytest.MonkeyPatch, built: list[Any]) -> None:
        from wactorz.node import cli as node_cli

        class _Runner:
            def __init__(self, broker: str, port: int, node_name: str) -> None:
                self.broker, self.port, self.node_name = broker, port, node_name
                built.append(self)

            async def run(self) -> None:
                return None

            async def shutdown(self) -> None:
                return None

        monkeypatch.setattr(node_cli, "NodeRunner", _Runner)

    def test_it_runs_the_node_it_was_named(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from wactorz.node import cli as node_cli

        built: list[Any] = []
        self._runner(monkeypatch, built)

        node_cli.run(self._args())

        assert (built[0].node_name, built[0].broker, built[0].port) == ("rpi", "broker.lan", 8883)

    def test_it_can_be_stopped_by_a_signal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import signal

        from wactorz.node import cli as node_cli

        built: list[Any] = []
        self._runner(monkeypatch, built)
        handled: list[int] = []
        real_new_loop = asyncio.new_event_loop

        def _loop() -> Any:
            loop = real_new_loop()
            real_add = loop.add_signal_handler

            def _add(sig: int, handler: Any) -> None:
                handled.append(sig)
                real_add(sig, handler)

            loop.add_signal_handler = _add  # type: ignore[method-assign]
            return loop

        monkeypatch.setattr(node_cli.asyncio, "new_event_loop", _loop)

        node_cli.run(self._args())

        # Both, because a node is stopped by `systemctl stop` and by Ctrl-C.
        assert set(handled) == {signal.SIGINT, signal.SIGTERM}

    def test_a_platform_without_signal_handlers_still_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows supports almost none of them; the node is stopped from
        # outside there instead, and must not refuse to start over it.
        from wactorz.node import cli as node_cli

        built: list[Any] = []
        self._runner(monkeypatch, built)
        real_new_loop = asyncio.new_event_loop

        def _loop() -> Any:
            loop = real_new_loop()

            def _refuse(*_a: Any, **_kw: Any) -> None:
                raise NotImplementedError

            loop.add_signal_handler = _refuse  # type: ignore[method-assign]
            return loop

        monkeypatch.setattr(node_cli.asyncio, "new_event_loop", _loop)

        node_cli.run(self._args())

        assert built, "the node never started"

    def test_a_name_that_cannot_be_a_topic_stops_before_anything_connects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wactorz.node import cli as node_cli

        built: list[Any] = []
        self._runner(monkeypatch, built)

        with pytest.raises(SystemExit) as exited:
            node_cli.run(self._args(node="rpi/kitchen"))

        assert exited.value.code == 2
        assert built == [], "a runner was built for a node that can never work"


class TestWhatACommandSchedules:
    """The handlers hand the work to a task and return.

    Deliberately: the subscriber is a sequential consumer, so a handler that
    awaited its work would stop every other message being delivered — including
    the reply the work is waiting for. What is worth pinning is that the right
    work is scheduled, and that the payload shapes main actually sends are read.
    """

    @staticmethod
    def _capture(runner: NodeRunner, name: str, calls: list[Any]) -> None:
        async def _work(*args: Any, **kwargs: Any) -> None:
            calls.append((args, kwargs))

        setattr(runner, name, _work)

    async def test_a_stop_names_the_agent(self, runner: NodeRunner) -> None:
        calls: list[Any] = []
        self._capture(runner, "stop_agent", calls)

        await runner._on_stop("nodes/rpi/stop", {"name": "collector"}, _Message("x"))
        await _settle()

        assert calls == [(("collector",), {"delete": False})]

    async def test_a_stop_can_ask_for_a_delete(self, runner: NodeRunner) -> None:
        calls: list[Any] = []
        self._capture(runner, "stop_agent", calls)

        await runner._on_stop("nodes/rpi/stop", {"name": "c", "delete": True}, _Message("x"))
        await _settle()

        assert calls == [(("c",), {"delete": True})]

    async def test_a_bare_name_is_still_a_stop(self, runner: NodeRunner) -> None:
        # What an older main sends. Dropping it would make its command vanish.
        calls: list[Any] = []
        self._capture(runner, "stop_agent", calls)

        await runner._on_stop("nodes/rpi/stop", "collector", _Message("x"))
        await _settle()

        assert calls == [(("collector",), {"delete": False})]

    async def test_a_stop_with_no_name_does_nothing(self, runner: NodeRunner) -> None:
        calls: list[Any] = []
        self._capture(runner, "stop_agent", calls)

        await runner._on_stop("nodes/rpi/stop", {}, _Message("x"))
        await _settle()

        assert calls == []

    async def test_stop_all_shuts_the_node_down(self, runner: NodeRunner) -> None:
        calls: list[Any] = []
        self._capture(runner, "shutdown", calls)

        await runner._on_stop_all("nodes/rpi/stop_all", None, _Message("x"))
        await _settle()

        assert len(calls) == 1

    async def test_restart_re_execs_the_process(self, runner: NodeRunner) -> None:
        calls: list[Any] = []
        self._capture(runner, "_restart", calls)

        await runner._on_restart("nodes/rpi/restart", None, _Message("x"))
        await _settle()

        assert len(calls) == 1

    async def test_restarting_one_agent_names_it(self, runner: NodeRunner) -> None:
        calls: list[Any] = []
        self._capture(runner, "_restart_agent", calls)

        await runner._on_restart_agent("nodes/rpi/restart_agent", {"name": "c"}, _Message("x"))
        await _settle()

        assert calls == [(("c",), {})]

    async def test_a_migrate_carries_the_whole_payload(self, runner: NodeRunner) -> None:
        calls: list[Any] = []
        self._capture(runner, "_migrate_agent", calls)
        payload = {"name": "c", "target_node": "@main", "return_token": "t"}

        await runner._on_migrate("nodes/rpi/migrate", payload, _Message("x"))
        await _settle()

        assert calls == [((payload,), {})]

    async def test_a_migrate_that_is_not_a_payload_is_ignored(self, runner: NodeRunner) -> None:
        calls: list[Any] = []
        self._capture(runner, "_migrate_agent", calls)

        await runner._on_migrate("nodes/rpi/migrate", "collector", _Message("x"))
        await _settle()

        assert calls == []

    async def test_work_that_fails_says_so_rather_than_vanishing(
        self, runner: NodeRunner, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A bare task drops its exception on the floor, so a spawn that raised
        # looked exactly like one that was never sent.
        async def _explode() -> None:
            raise RuntimeError("the camera is not there")

        with caplog.at_level("ERROR"):
            runner._background(_explode(), "spawn_agent")
            await _settle()

        assert "spawn_agent failed" in caplog.text
        assert "the camera is not there" in caplog.text


async def _settle() -> None:
    """Let the tasks a handler scheduled run."""
    for _ in range(6):
        await asyncio.sleep(0)


class TestInstallingWhatAnAgentNeeds:
    """A spawn config may name packages the agent imports.

    They arrive over the broker, so what may be installed is settled elsewhere
    (`test_installer_package_names`). What matters here is that a pip which
    misbehaves cannot take the node with it: a spawn waits on this, and the
    control connection waits on the spawn.
    """

    class _Proc:
        def __init__(self, returncode: int = 0, hang: bool = False) -> None:
            self.returncode = returncode
            self._hang = hang
            self.killed = False
            self.waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            if self._hang:
                await asyncio.Event().wait()
            return b"", b"could not find a version"

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            return -9

    def _pip(self, monkeypatch: pytest.MonkeyPatch, proc: Any) -> list[tuple[Any, ...]]:
        calls: list[tuple[Any, ...]] = []

        async def _exec(*args: Any, **_kw: Any) -> Any:
            calls.append(args)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _exec)
        return calls

    async def test_a_good_install_reports_nothing_refused(
        self, runner: NodeRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._pip(monkeypatch, self._Proc())

        assert await runner._install_packages(["requests"]) == []
        assert "requests" in calls[0]

    async def test_a_failing_install_is_logged_and_the_spawn_goes_on(
        self, runner: NodeRunner, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A pip failure can be transient, and the agent may not even need the
        # package yet — unlike a refusal, which means we read the request and
        # rejected it, and stops the spawn.
        self._pip(monkeypatch, self._Proc(returncode=1))

        with caplog.at_level("WARNING"):
            assert await runner._install_packages(["requests"]) == []

        assert "pip install warning" in caplog.text

    async def test_a_pip_that_never_returns_is_given_up_on(
        self, runner: NodeRunner, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        proc = self._Proc(hang=True)
        self._pip(monkeypatch, proc)
        monkeypatch.setattr(runner_mod, "INSTALL_TIMEOUT_S", 0.05)

        with caplog.at_level("WARNING"):
            assert await asyncio.wait_for(runner._install_packages(["requests"]), timeout=5) == []

        assert proc.killed and proc.waited, "the process was left running"
        assert "gave up" in caplog.text
