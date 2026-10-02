"""A failure that is carried on from is still said.

Three places went on in silence. An actor's stop swallowed whatever its
`on_stop` or its state save raised, so an agent that lost its state at shutdown
left no trace of why. A listener that lost its broker connection retried for
ever without a word: an agent that no longer took commands over the broker, or
a stream window that had stopped filling, looked like one with nothing to do.
And the dashboard's clean-up after a delete ran as a task nothing held, which
the event loop is free to drop part-way.

Each still carries on. What changed is that the log says so, once when it goes
wrong and once when it is right again, not at every retry.
"""

import asyncio
import logging
import re
from pathlib import Path

import pytest

from tests.waiting import until
from wactorz.core import actor as actor_module
from wactorz.core import mqtt as core_mqtt
from wactorz.core import topic_bus
from wactorz.core.actor import Actor, ActorState, Message
from wactorz.core.topic_bus import StreamWindow
from wactorz.web import lifecycle


class _Worker(Actor):
    async def handle_message(self, msg: Message) -> None:
        return None


class TestStoppingAnActor:
    async def test_a_failing_on_stop_is_logged_and_the_stop_completes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        saved: list[bool] = []

        class _Clumsy(_Worker):
            async def on_stop(self) -> None:
                raise RuntimeError("the camera would not close")

            async def _save_persistent_state(self) -> None:
                saved.append(True)

        actor = _Clumsy(name="clumsy")
        await actor.start()

        await actor.stop()

        assert "[clumsy] on_stop failed; stopping anyway" in caplog.text
        assert "the camera would not close" in caplog.text
        assert saved == [True], "its state is still saved"
        assert actor.state == ActorState.STOPPED

    async def test_a_state_save_that_fails_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _Unsaved(_Worker):
            async def _save_persistent_state(self) -> None:
                raise OSError("No space left on device")

        actor = _Unsaved(name="unsaved")
        await actor.start()

        await actor.stop()

        assert "[unsaved] Could not save state while stopping" in caplog.text
        assert "No space left on device" in caplog.text
        assert actor.state == ActorState.STOPPED


def _said(caplog: pytest.LogCaptureFixture, text: str) -> int:
    return sum(text in record.getMessage() for record in caplog.records)


class _Refusing:
    """Stands in for the broker connection: refuses, and counts how often it was tried."""

    def __init__(self) -> None:
        self.attempts = 0

    def __call__(self, *_args: object, **_kwargs: object) -> "_Refusing":
        return self

    async def __aenter__(self) -> None:
        self.attempts += 1
        raise OSError("Connection refused")

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.fixture(name="broker")
def broker_fixture(monkeypatch: pytest.MonkeyPatch) -> _Refusing:
    refusing = _Refusing()
    monkeypatch.setattr(core_mqtt, "mqtt_client", refusing)
    return refusing


class TestALostBrokerConnection:
    async def test_an_actors_command_listener_says_so_once(
        self, broker: _Refusing, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(actor_module, "RECONNECT_DELAY_S", 0.01)
        actor = _Worker(name="worker")
        actor.state = ActorState.RUNNING
        caplog.set_level(logging.WARNING, logger="wactorz.core.actor")

        listening = asyncio.create_task(actor._command_listener())
        try:
            await until(lambda: broker.attempts >= 5, "the listener trying the broker again")
        finally:
            listening.cancel()
            await asyncio.gather(listening, return_exceptions=True)

        assert _said(caplog, "[worker] Lost the broker connection it takes commands on") == 1
        assert _said(caplog, "Connection refused") == 1

    async def test_a_stream_window_says_so_once(
        self, broker: _Refusing, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_bus, "WINDOW_RECONNECT_DELAY_S", 0.01)
        window = StreamWindow("sensors/temp", seconds=60)
        caplog.set_level(logging.WARNING, logger="wactorz.core.topic_bus")

        window.start("localhost", 1883)
        task = window._task
        assert task is not None
        try:
            await until(lambda: broker.attempts >= 5, "the window trying the broker again")
        finally:
            window.stop()
            await asyncio.gather(task, return_exceptions=True)

        assert _said(caplog, "Lost the broker connection reading sensors/temp") == 1


class TestWorkStartedAndNotWaitedFor:
    async def test_it_is_held_until_it_ends(self) -> None:
        release = asyncio.Event()

        async def _work() -> None:
            await release.wait()

        task = lifecycle._in_background(_work())

        assert task in lifecycle._background
        release.set()
        await task
        await until(lambda: task not in lifecycle._background, "the finished task being let go")

    async def test_a_failure_in_it_is_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        async def _work() -> None:
            raise RuntimeError("the broker hung up")

        task = lifecycle._in_background(_work())
        await asyncio.gather(task, return_exceptions=True)
        await until(lambda: task not in lifecycle._background, "the finished task being let go")

        assert "Background work failed" in caplog.text
        assert "the broker hung up" in caplog.text


class TestReconnectingIsSpread:
    """Every actor holds a broker connection; dropped together, they must not return together."""

    def test_the_wait_is_the_delay_and_a_little_more_and_not_the_same_twice(self) -> None:
        waits = [core_mqtt.reconnect_wait(10.0) for _ in range(200)]

        assert all(10.0 <= wait <= 15.0 for wait in waits)
        assert len(set(waits)) > 100

    @pytest.mark.parametrize(
        "module",
        [
            "core/actor.py",
            "core/topic_bus.py",
            "core/mqtt_publisher.py",
            "web/mqtt.py",
            "node/runner.py",
            "agents/main/nodes.py",
        ],
    )
    def test_each_reconnect_loop_uses_it(self, module: str) -> None:
        source = (Path(core_mqtt.__file__).parents[1] / module).read_text(encoding="utf-8")

        assert re.search(r"asyncio\.sleep\(reconnect_wait\(|wait = reconnect_wait\(", source)
