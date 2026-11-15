# SPDX-License-Identifier: Apache-2.0
"""An actor whose start fails is not left registered as running with nothing running.

`Actor.start` marks the actor running before ``on_start`` and starts its loops
last, so a start that fails part-way would leave it listed and reported as
running. `start_registered` winds such an actor back down, and every way into
the system starts actors through it: `ActorSystem.spawn`, `ActorSystem.start`
and the supervisor.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.waiting import until
from wactorz.core import registry as registry_module
from wactorz.core.actor import Actor, ActorState, Message
from wactorz.core.registry import ActorRegistry, ActorSystem, start_registered


class StartFailed(Exception):
    """What a test's ``on_start`` fails with, so its propagation can be told apart."""


class _Worker(Actor):
    """An actor that starts, and does nothing."""

    async def handle_message(self, message: Message) -> None:
        return None


class _Failing(_Worker):
    """An actor whose ``on_start`` raises."""

    async def on_start(self) -> None:
        raise StartFailed("the sensor is not there")


class _Hanging(_Worker):
    """An actor whose ``on_start`` waits until it is cancelled."""

    started: asyncio.Event

    async def on_start(self) -> None:
        self.started.set()
        await asyncio.Event().wait()


class _Publisher:
    """Stands in for `MQTTPublisher`, which needs a broker."""

    async def disconnect(self) -> None:
        return None


def _worker(cls: type[_Worker], tmp_path: Path, name: str) -> Any:
    return cls(name=name, persistence_dir=str(tmp_path / name))


class TestStartRegistered:
    async def test_a_start_that_works_leaves_the_actor_registered_and_running(
        self, tmp_path: Path
    ) -> None:
        registry = ActorRegistry()
        actor = _worker(_Worker, tmp_path, "w")

        await start_registered(registry, actor)
        try:
            assert registry.find_by_name("w") is actor
            assert actor.state == ActorState.RUNNING
        finally:
            await actor.stop()

    async def test_a_failed_start_is_stopped_unregistered_and_its_error_raised(
        self, tmp_path: Path
    ) -> None:
        registry = ActorRegistry()
        actor = _worker(_Failing, tmp_path, "f")

        with pytest.raises(StartFailed, match="the sensor is not there"):
            await start_registered(registry, actor)

        assert registry.find_by_name("f") is None
        assert actor.state == ActorState.STOPPED

    async def test_a_cancelled_start_is_wound_back_and_the_cancellation_raised(
        self, tmp_path: Path
    ) -> None:
        registry = ActorRegistry()
        actor = _worker(_Hanging, tmp_path, "h")
        actor.started = asyncio.Event()
        starting = asyncio.create_task(start_registered(registry, actor))
        await asyncio.wait_for(actor.started.wait(), timeout=10)

        starting.cancel()
        (outcome,) = await asyncio.gather(starting, return_exceptions=True)

        assert isinstance(outcome, asyncio.CancelledError)
        assert registry.find_by_name("h") is None
        assert actor.state == ActorState.STOPPED

    async def test_a_stop_that_fails_still_unregisters_and_keeps_the_starts_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = ActorRegistry()
        actor = _worker(_Failing, tmp_path, "f")

        async def broken_stop() -> None:
            raise RuntimeError("stop broke too")

        monkeypatch.setattr(actor, "stop", broken_stop)

        with pytest.raises(StartFailed):
            await start_registered(registry, actor)

        assert registry.find_by_name("f") is None


class TestTheSystem:
    @pytest.fixture(name="system")
    def system_fixture(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ActorSystem:
        async def create(*_args: Any, **_kwargs: Any) -> _Publisher:
            return _Publisher()

        monkeypatch.setattr(registry_module.MQTTPublisher, "create", create)
        return ActorSystem(state_dir=str(tmp_path / "state"))

    async def test_a_spawn_whose_start_fails_is_not_left_running(
        self, system: ActorSystem, tmp_path: Path
    ) -> None:
        with pytest.raises(StartFailed):
            await system.spawn(_Failing, name="f", persistence_dir=str(tmp_path / "f"))

        assert system.registry.find_by_name("f") is None
        assert system.registry.all_actors() == []

    async def test_a_spawn_that_starts_is_registered_and_running(
        self, system: ActorSystem, tmp_path: Path
    ) -> None:
        actor = await system.spawn(_Worker, name="w", persistence_dir=str(tmp_path / "w"))
        try:
            assert system.registry.find_by_name("w") is actor
            assert actor.state == ActorState.RUNNING
        finally:
            await system.stop_all()

    async def test_starting_winds_back_the_one_that_failed_and_leaves_the_others(
        self, system: ActorSystem, tmp_path: Path
    ) -> None:
        good = _worker(_Worker, tmp_path, "good")
        bad = _worker(_Failing, tmp_path, "bad")
        try:
            with pytest.raises(StartFailed):
                await system.start(good, bad)

            assert system.registry.find_by_name("bad") is None
            assert bad.state == ActorState.STOPPED
            assert system.registry.find_by_name("good") is good
            assert good.state == ActorState.RUNNING
        finally:
            await system.stop_all()

        await until(lambda: good.state == ActorState.STOPPED, "the survivor stopped by stop_all")
