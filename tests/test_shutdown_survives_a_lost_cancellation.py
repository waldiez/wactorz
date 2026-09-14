"""Shutdown finishes even when a cancellation is lost.

On Python 3.10 and 3.11 ``asyncio.wait_for`` returns its result from inside its
own ``CancelledError`` handler when the future it guards completes in the same
instant the caller is cancelled. aiomqtt subscribes and publishes through
``wait_for``, so a task holding a broker connection can lose the request to stop
and go on waiting for messages for ever. The monitor's listener and the
publisher's drain loop both hold one, and a wait on either without a limit —
``asyncio.run``'s on the way out, ``disconnect()``'s on the way down — does not end.

These pin the guarantee rather than the interpreter's behaviour: a cancellation
that does not take is asked again, a caller's own cancellation still reaches it,
and a task that will not stop is not waited on for ever.
"""

import argparse
import asyncio
import signal
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from wactorz import app as app_mod
from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core import cancellation, mqtt_publisher, persistence, registry
from wactorz.core.actor import Actor, ActorState, Message
from wactorz.core.cancellation import cancel_until_done
from wactorz.core.mqtt_publisher import MQTTPublisher
from wactorz.core.persistence import maintenance
from wactorz.web import app as web_app
from wactorz.web import runtime


async def _forever() -> None:
    await asyncio.sleep(3600)


async def _loses_its_first_cancellation() -> None:
    """Carries on after one cancellation, as the listener does once wait_for drops it."""
    try:
        await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(3600)


async def _refusing(give_up: asyncio.Event) -> asyncio.Task[None]:
    """A running task that ignores every cancellation until `give_up` is set.

    Returned only once it is inside its loop. A task cancelled before its first
    step never reaches its ``except``, it simply ends, and whether a caller's
    first cancellation lands before that step depends on the interpreter: from
    Python 3.12 ``asyncio.wait_for`` runs its coroutine in the calling task instead
    of scheduling a new one behind this.
    """
    running = asyncio.Event()

    async def refuses() -> None:
        running.set()
        while not give_up.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

    task = asyncio.ensure_future(refuses())
    await running.wait()
    return task


class TestCancelUntilDone:
    async def test_the_listeners_race_is_survived(self) -> None:
        """A wait_for whose future completes in the instant the stop request lands."""
        loop = asyncio.get_running_loop()
        subscribed: asyncio.Future[None] = loop.create_future()

        async def subscribe_then_listen() -> None:
            await asyncio.wait_for(subscribed, 10)
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(subscribe_then_listen())
        await asyncio.sleep(0)  # parked inside wait_for
        subscribed.set_result(None)  # the subscribe completes, and in the same instant:
        assert await cancel_until_done(task, timeout=5.0, recancel_after=0.05)
        assert task.done()

    async def test_a_task_that_lost_its_cancellation_still_stops(self) -> None:
        task = asyncio.ensure_future(_loses_its_first_cancellation())
        await asyncio.sleep(0)
        assert await cancel_until_done(task, timeout=5.0, recancel_after=0.05)
        assert task.cancelled()

    async def test_a_task_that_stops_is_not_kept_waiting(self) -> None:
        task = asyncio.ensure_future(_forever())
        await asyncio.sleep(0)
        started = time.monotonic()
        assert await cancel_until_done(task, timeout=30.0, recancel_after=10.0)
        assert time.monotonic() - started < 1.0

    async def test_a_task_that_will_not_stop_is_given_up_on(self) -> None:
        give_up = asyncio.Event()
        task = await _refusing(give_up)
        try:
            gave_up = await asyncio.wait_for(
                cancel_until_done(task, timeout=0.2, recancel_after=0.05), timeout=5.0
            )
            assert gave_up is False
        finally:
            give_up.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_the_callers_own_cancellation_still_reaches_it(self) -> None:
        stubborn = asyncio.ensure_future(_loses_its_first_cancellation())
        noticed = asyncio.Event()

        async def caller() -> None:
            try:
                await cancel_until_done(stubborn, timeout=30.0, recancel_after=10.0)
            except asyncio.CancelledError:
                noticed.set()
                raise

        waiting = asyncio.ensure_future(caller())
        await asyncio.sleep(0.05)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert noticed.is_set()
        stubborn.cancel()
        await asyncio.gather(stubborn, return_exceptions=True)

    async def test_a_task_that_already_failed_counts_as_stopped(self) -> None:
        async def fails() -> None:
            raise RuntimeError("boom")

        task = asyncio.ensure_future(fails())
        await asyncio.sleep(0)
        assert await cancel_until_done(task, timeout=1.0)


class TestTheMonitorAtShutdown:
    @pytest.fixture(autouse=True)
    def _keep_runtime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whatever a test sets on the shared runtime is put back afterwards."""
        for name in ("MQTT_BROKER", "MQTT_PORT", "WS_PORT", "server_task"):
            monkeypatch.setattr(runtime, name, getattr(runtime, name))
        monkeypatch.setattr(cancellation, "RECANCEL_AFTER_S", 0.05)

    async def test_starting_the_monitor_keeps_its_task_for_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started = asyncio.Event()

        async def fake_server() -> None:
            started.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(web_app, "main", fake_server)
        # _start_web_ui quietens the web loggers process-wide; not in a test suite.
        monkeypatch.setattr(app_mod.logging, "getLogger", lambda *_args: MagicMock())

        await app_mod._start_web_ui(port=8999, mqtt_broker="localhost", mqtt_port=1883)
        task = runtime.server_task
        assert task is not None
        await asyncio.wait_for(started.wait(), timeout=5.0)

        await asyncio.wait_for(app_mod._stop_web_ui(), timeout=5.0)
        assert task.done()
        assert runtime.server_task is None

    async def test_shutdown_stops_a_monitor_that_lost_its_cancellation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        task = asyncio.ensure_future(_loses_its_first_cancellation())
        await asyncio.sleep(0)
        monkeypatch.setattr(runtime, "server_task", task)

        await asyncio.wait_for(app_mod._stop_web_ui(), timeout=5.0)

        assert task.cancelled()
        assert runtime.server_task is None

    async def test_a_monitor_that_will_not_stop_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        give_up = asyncio.Event()
        task = await _refusing(give_up)
        logger = MagicMock()
        monkeypatch.setattr(runtime, "server_task", task)
        monkeypatch.setattr(app_mod, "MONITOR_STOP_TIMEOUT_S", 0.2)
        monkeypatch.setattr(app_mod, "logger", logger)
        try:
            await asyncio.wait_for(app_mod._stop_web_ui(), timeout=5.0)
        finally:
            give_up.set()
            await asyncio.gather(task, return_exceptions=True)
        assert logger.warning.called

    async def test_with_no_monitor_there_is_nothing_to_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(runtime, "server_task", None)
        await asyncio.wait_for(app_mod._stop_web_ui(), timeout=5.0)
        assert runtime.server_task is None


class TestWhateverIsLeftAtShutdown:
    @pytest.fixture(autouse=True)
    def _quick_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cancellation, "RECANCEL_AFTER_S", 0.05)

    async def test_leftover_tasks_that_lose_a_cancellation_still_stop(self) -> None:
        tasks = [asyncio.ensure_future(_loses_its_first_cancellation()) for _ in range(3)]
        await asyncio.sleep(0)
        await asyncio.wait_for(app_mod._stop_tasks(tasks, timeout=5.0), timeout=10.0)
        assert all(task.cancelled() for task in tasks)

    async def test_a_leftover_that_will_not_stop_is_named(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        give_up = asyncio.Event()
        task = await _refusing(give_up)
        task.set_name("stuck-window")
        logger = MagicMock()
        monkeypatch.setattr(app_mod, "logger", logger)
        try:
            await asyncio.wait_for(app_mod._stop_tasks([task], timeout=0.2), timeout=5.0)
        finally:
            give_up.set()
            await asyncio.gather(task, return_exceptions=True)
        assert "stuck-window" in str(logger.warning.call_args)

    async def test_finished_tasks_are_left_alone(self) -> None:
        async def done() -> None:
            return None

        task = asyncio.ensure_future(done())
        await task
        await asyncio.wait_for(app_mod._stop_tasks([task]), timeout=5.0)
        assert not task.cancelled()


def test_everything_but_the_shutdown_itself_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run in a loop of its own, where every other task is one this test made."""
    monkeypatch.setattr(cancellation, "RECANCEL_AFTER_S", 0.05)

    async def scenario() -> bool:
        other = asyncio.ensure_future(_loses_its_first_cancellation())
        await asyncio.sleep(0)
        # Awaited directly, as app() awaits it: wrapped in wait_for it would run in
        # a task of its own and stop this one. Returning at all is the other half
        # of the claim, that the task running the shutdown is not one it stops.
        # Bounded without an outer timeout, by the helper's own limit.
        await app_mod._stop_leftover_tasks()
        return other.cancelled()

    assert asyncio.run(scenario())


class TestThePublisherAtShutdown:
    @pytest.fixture(autouse=True)
    def _quick_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cancellation, "RECANCEL_AFTER_S", 0.05)

    async def test_a_drain_loop_that_lost_its_cancellation_still_stops(
        self, tmp_path: Path
    ) -> None:
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        task = asyncio.ensure_future(_loses_its_first_cancellation())
        pub._task = task
        await asyncio.sleep(0)

        await asyncio.wait_for(pub.disconnect(), timeout=5.0)

        assert task.cancelled()

    async def test_a_drain_loop_that_will_not_stop_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        give_up = asyncio.Event()
        pub = MQTTPublisher(db_path=str(tmp_path / "outbox.db"))
        pub._task = await _refusing(give_up)
        logger = MagicMock()
        monkeypatch.setattr(mqtt_publisher, "DRAIN_STOP_TIMEOUT_S", 0.2)
        monkeypatch.setattr(mqtt_publisher, "logger", logger)
        try:
            await asyncio.wait_for(pub.disconnect(), timeout=5.0)
        finally:
            give_up.set()
            await asyncio.gather(pub._task, return_exceptions=True)
        assert "did not stop" in str(logger.warning.call_args)


class TestAStopDuringStartup:
    """A stop that arrives before app() reaches the try whose finally shuts down."""

    @pytest.fixture(name="shut_down")
    def shut_down_fixture(self, monkeypatch: pytest.MonkeyPatch) -> list[object]:
        calls: list[object] = []

        async def record(system: object) -> None:
            calls.append(system)

        monkeypatch.setattr(app_mod, "_shut_down", record)
        return calls

    async def test_the_part_that_started_is_shut_down(
        self, monkeypatch: pytest.MonkeyPatch, shut_down: list[object]
    ) -> None:
        half_built = object()
        entered = asyncio.Event()

        async def build_system(
            _args: argparse.Namespace, on_system: Callable[[object], object] | None = None
        ) -> None:
            assert on_system is not None
            on_system(half_built)
            entered.set()
            await asyncio.sleep(3600)  # an agent still starting when the stop arrives

        monkeypatch.setattr(app_mod, "build_system", build_system)
        starting = asyncio.ensure_future(app_mod._build_system_or_stop(argparse.Namespace()))
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        starting.cancel()

        with pytest.raises(asyncio.CancelledError):
            await starting
        assert shut_down == [half_built]

    async def test_a_stop_before_the_system_exists_still_shuts_down(
        self, monkeypatch: pytest.MonkeyPatch, shut_down: list[object]
    ) -> None:
        entered = asyncio.Event()

        async def build_system(
            _args: argparse.Namespace, on_system: Callable[[object], object] | None = None
        ) -> None:
            entered.set()
            await asyncio.sleep(3600)  # still choosing a provider, say

        monkeypatch.setattr(app_mod, "build_system", build_system)
        starting = asyncio.ensure_future(app_mod._build_system_or_stop(argparse.Namespace()))
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        starting.cancel()

        with pytest.raises(asyncio.CancelledError):
            await starting
        assert shut_down == [None]

    async def test_a_startup_that_fails_is_shut_down_and_still_raises(
        self, monkeypatch: pytest.MonkeyPatch, shut_down: list[object]
    ) -> None:
        half_built = object()

        async def build_system(
            _args: argparse.Namespace, on_system: Callable[[object], object] | None = None
        ) -> None:
            assert on_system is not None
            on_system(half_built)
            raise RuntimeError("broker refused")

        monkeypatch.setattr(app_mod, "build_system", build_system)
        with pytest.raises(RuntimeError, match="broker refused"):
            await app_mod._build_system_or_stop(argparse.Namespace())
        assert shut_down == [half_built]

    async def test_a_finished_startup_is_not_shut_down(
        self, monkeypatch: pytest.MonkeyPatch, shut_down: list[object]
    ) -> None:
        async def build_system(
            _args: argparse.Namespace, on_system: Callable[[object], object] | None = None
        ) -> tuple[str, str, str]:
            return ("system", "main", "db")

        monkeypatch.setattr(app_mod, "build_system", build_system)
        built = await app_mod._build_system_or_stop(argparse.Namespace())
        assert built == ("system", "main", "db")
        assert shut_down == []


class TestTheShutdownSequence:
    @pytest.fixture(name="calls")
    def calls_fixture(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        calls: list[str] = []

        async def stop_maintenance() -> None:
            calls.append("maintenance")

        async def stop_web_ui() -> None:
            calls.append("monitor")

        def close_persistence() -> None:
            calls.append("database")

        async def stop_leftovers() -> None:
            calls.append("leftovers")

        monkeypatch.setattr(maintenance, "stop", stop_maintenance)
        monkeypatch.setattr(persistence, "close_persistence", close_persistence)
        monkeypatch.setattr(app_mod, "_stop_web_ui", stop_web_ui)
        monkeypatch.setattr(app_mod, "_stop_leftover_tasks", stop_leftovers)
        return calls

    async def test_it_runs_in_the_order_that_keeps_state_intact(self, calls: list[str]) -> None:
        class _System:
            async def stop_all(self) -> None:
                calls.append("agents")

        await app_mod._shut_down(_System())  # pyright: ignore[reportArgumentType]
        assert calls == ["maintenance", "agents", "monitor", "database", "leftovers"]

    async def test_without_a_system_the_rest_still_runs(self, calls: list[str]) -> None:
        await app_mod._shut_down(None)
        assert calls == ["maintenance", "monitor", "database", "leftovers"]

    async def test_it_marks_shutdown_as_begun_before_its_first_step(
        self, calls: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bool] = []

        async def stop_maintenance() -> None:
            seen.append(app_mod._shutting_down.is_set())

        app_mod._shutting_down.clear()
        monkeypatch.setattr(maintenance, "stop", stop_maintenance)
        await app_mod._shut_down(None)
        assert seen == [True]


async def test_build_system_hands_the_system_over_before_starting_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Handed over before the first await on it, so a stop from then on can reach it."""

    class _HalfBuilt:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    async def refuse(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the broker is not there")

    monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "ActorSystem", _HalfBuilt)
    monkeypatch.setattr(MQTTPublisher, "create", refuse)
    seen: list[object] = []
    args = argparse.Namespace(
        llm="fake",
        ollama_model=None,
        nim_model=None,
        gemini_model=None,
        mqtt_broker="localhost",
        mqtt_port=1883,
    )

    with pytest.raises(RuntimeError, match="the broker is not there"):
        await app_mod.build_system(args, on_system=seen.append)

    assert len(seen) == 1
    assert isinstance(seen[0], _HalfBuilt)


class TestTheSignalHandlerAsksAgain:
    @pytest.fixture(name="handlers")
    async def handlers_fixture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> dict[int, Callable[..., object]]:
        """The handlers _install_signal_handlers registers, captured instead of installed."""
        handlers: dict[int, Callable[..., object]] = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop, "add_signal_handler", lambda sig, cb, *_args: handlers.__setitem__(sig, cb)
        )
        monkeypatch.setattr(cancellation, "RECANCEL_AFTER_S", 0.05)
        return handlers

    async def test_a_stop_request_startup_loses_is_asked_again(
        self, handlers: dict[int, Callable[..., object]]
    ) -> None:
        installed = asyncio.Event()

        async def startup_that_loses_one_request() -> str:
            app_mod._install_signal_handlers()
            installed.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                pass  # discarded inside a wait_for, as startup can discard it
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return "stopped"
            return "ran on"

        task = asyncio.ensure_future(startup_that_loses_one_request())
        await asyncio.wait_for(installed.wait(), timeout=5.0)
        handlers[signal.SIGINT]()
        assert await asyncio.wait_for(task, timeout=5.0) == "stopped"

    async def test_asking_stops_once_shutdown_has_begun(
        self, handlers: dict[int, Callable[..., object]]
    ) -> None:
        installed = asyncio.Event()

        async def app_that_shuts_down() -> str:
            app_mod._install_signal_handlers()
            installed.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                app_mod._shutting_down.set()  # what _shut_down does first
                try:
                    await asyncio.sleep(0.3)  # a shutdown that takes several intervals
                except asyncio.CancelledError:
                    return "interrupted mid-shutdown"
                return "shut down"
            return "never asked"

        task = asyncio.ensure_future(app_that_shuts_down())
        await asyncio.wait_for(installed.wait(), timeout=5.0)
        handlers[signal.SIGTERM]()
        assert await asyncio.wait_for(task, timeout=5.0) == "shut down"


class _Quiet(Actor):
    async def handle_message(self, msg: Message) -> None:
        return None


class TestAnAgentThatLostACancellation:
    @pytest.fixture(autouse=True)
    def _quick_retries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cancellation, "RECANCEL_AFTER_S", 0.05)

    async def test_its_tasks_stop_without_waiting_out_the_timeout(self, tmp_path: Path) -> None:
        actor = _Quiet(name="quiet", persistence_dir=str(tmp_path))
        actor.state = ActorState.RUNNING
        lost = asyncio.ensure_future(_loses_its_first_cancellation())
        actor._tasks = [lost]
        await asyncio.sleep(0)

        started = time.monotonic()
        await asyncio.wait_for(actor._wind_down_tasks(), timeout=10.0)

        assert lost.cancelled()
        assert time.monotonic() - started < actor.TASK_SHUTDOWN_TIMEOUT / 2
        assert not actor._tasks

    async def test_a_replaced_programs_tasks_stop_the_same_way(self, tmp_path: Path) -> None:
        agent = DynamicAgent(
            name="repairing",
            code="async def process(agent):\n    pass\n",
            poll_interval=0,
            persistence_dir=str(tmp_path),
        )
        agent.state = ActorState.RUNNING
        lost = asyncio.ensure_future(_loses_its_first_cancellation())
        agent._program_tasks = [lost]
        await asyncio.sleep(0)

        started = time.monotonic()
        await asyncio.wait_for(agent._tear_down_program(), timeout=10.0)

        assert lost.cancelled()
        assert time.monotonic() - started < agent.TASK_SHUTDOWN_TIMEOUT / 2


class TestStoppingSeveralTasksTogether:
    async def test_only_the_ones_still_running_are_returned(self) -> None:
        give_up = asyncio.Event()
        stubborn = await _refusing(give_up)
        ordinary = asyncio.ensure_future(_forever())
        await asyncio.sleep(0)
        try:
            stuck = await asyncio.wait_for(
                cancellation.cancel_all_until_done([stubborn, ordinary], timeout=0.2), timeout=5.0
            )
        finally:
            give_up.set()
            await asyncio.gather(stubborn, return_exceptions=True)
        assert stuck == [stubborn]
        assert ordinary.cancelled()

    async def test_a_task_that_already_failed_is_not_reported(self) -> None:
        async def fails() -> None:
            raise RuntimeError("boom")

        failed = asyncio.ensure_future(fails())
        await asyncio.sleep(0)
        assert await cancellation.cancel_all_until_done([failed], timeout=1.0) == []
