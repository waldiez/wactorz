"""`/agents stop|restart` must mean what they say.

Each verb routes through `apply_command`, which is where the rules live: a stop
releases supervision before it stops, so the watchdog does not read the silence
as a crash, and a protected agent refuses rather than being reported as done.

Both paths now go through ``Actor.apply_command``, which is also what releases a
deliberately stopped actor from supervision.
"""

from typing import Any

import pytest

from wactorz.core.actor import Actor, ActorState, Message
from wactorz.core.registry import ActorRegistry, Supervisor


class _Recorder(Actor):
    """An actor that records lifecycle calls rather than performing them."""

    def __init__(self, name: str = "worker", protected: bool = False) -> None:
        super().__init__(name=name)
        self.protected = protected
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")
        self.state = ActorState.STOPPED

    async def handle_message(self, message: Message) -> None:
        return None


class _Supervisor:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, name: str) -> None:
        self.released.append(name)

    def resupervise(self, name: str, _actor: Actor) -> None:
        return None


class _Registry:
    def __init__(self, actor: Actor) -> None:
        self._actor = actor
        self.supervisor = _Supervisor()
        self._supervisor_ref = self.supervisor
        self.unregistered: list[str] = []

    def find_by_name(self, name: str) -> Actor | None:
        return self._actor if self._actor.name == name else None

    def get(self, actor_id: str) -> Actor | None:
        return self._actor if self._actor.actor_id == actor_id else None

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)


@pytest.fixture(name="agent")
def agent_fixture() -> _Recorder:
    """An actor wired to a stand-in registry with a supervisor behind it."""
    actor = _Recorder()
    actor._registry = _Registry(actor)  # type: ignore[assignment]
    return actor


def _registry_of(agent: _Recorder) -> Any:
    return agent._registry


class TestStopReleasesFirst:
    async def test_stopping_releases_from_supervision(self, agent: _Recorder) -> None:
        await agent.apply_command("stop")

        # Without this the watchdog reads the silence as a crash and restarts
        # the agent that was deliberately stopped.
        assert _registry_of(agent).supervisor.released == ["worker"]
        assert agent.calls == ["stop"]

    async def test_an_essential_agent_refuses_a_stop(self, agent: _Recorder) -> None:
        agent.essential = True

        # Protection covers delete; refusing a stop is a separate question and
        # only the agent that carries the way back answers yes to it.
        assert await agent.apply_command("stop") is False

        assert agent.calls == []
        assert _registry_of(agent).supervisor.released == []


class TestSupervisedFlagIsHonest:
    async def test_it_clears_when_the_actor_is_released(self) -> None:
        registry = ActorRegistry()
        supervisor = Supervisor(registry, lambda _actor: None)
        actor = _Recorder(name="watched")
        supervisor.supervise("watched", lambda: actor)
        supervisor._specs["watched"].actor = actor
        actor.supervisor_id = str(id(supervisor))

        assert actor.get_status()["supervised"] is True

        supervisor.release("watched")

        # release() used to leave the marker set, so a deliberately stopped
        # agent went on reporting itself supervised to the dashboard and to
        # /api/actors for the rest of its life.
        assert actor.get_status()["supervised"] is False

    async def test_it_returns_when_the_actor_is_supervised_again(self) -> None:
        registry = ActorRegistry()
        supervisor = Supervisor(registry, lambda _actor: None)
        actor = _Recorder(name="watched")
        supervisor.supervise("watched", lambda: actor)
        supervisor.release("watched")

        supervisor.resupervise("watched", actor)

        assert actor.get_status()["supervised"] is True
