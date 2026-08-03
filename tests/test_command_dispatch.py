"""A command from the dashboard must reach the actor, or say that it did not.

Local actors are driven in process. The web layer used to publish every command
to the broker so an actor it already held could receive its own command back over
the network — which meant that with the broker down, ``pause`` did nothing while
the response still said ``pausing`` and the dashboard kept showing the agent as
running. Nothing corrected that until the agent's next heartbeat.

The other half is the supervision release. Stopping an actor without releasing it
is indistinguishable from crashing, so the heartbeat watchdog restarts it moments
later — the delete path reached that state by calling ``stop()`` directly.
"""

import json

import pytest

from wactorz.core.actor import Actor, ActorState
from wactorz.web import api_actors, lifecycle, runtime, ws


class _Recorder(Actor):
    """An actor that records lifecycle calls instead of performing them."""

    def __init__(self, name: str = "worker", protected: bool = False) -> None:
        super().__init__(name=name)
        self.protected = protected
        self.calls: list[str] = []

    async def pause(self) -> None:
        self.calls.append("pause")

    async def resume(self) -> None:
        self.calls.append("resume")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def handle_message(self, message):  # pragma: no cover - never delivered
        return None


class _Supervisor:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, name: str) -> None:
        self.released.append(name)


class _Registry:
    def __init__(self, actor: Actor | None = None) -> None:
        self._actor = actor
        self._supervisor_ref = _Supervisor()
        self.unregistered: list[str] = []

    def get(self, actor_id: str) -> Actor | None:
        if self._actor is not None and self._actor.actor_id == actor_id:
            return self._actor
        return None

    def find_by_name(self, name: str) -> Actor | None:
        if self._actor is not None and self._actor.name == name:
            return self._actor
        return None

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, payload))


class _Request:
    """Just enough of ``web.Request`` for the handlers under test."""

    def __init__(self, actor_id: str) -> None:
        self.match_info = {"actor_id": actor_id}


@pytest.fixture(autouse=True)
def _restore_runtime():
    registry, client = runtime.registry, runtime.mqtt_client_ref
    yield
    runtime.registry, runtime.mqtt_client_ref = registry, client


def _payload(response) -> dict:
    return json.loads(response.body.decode())


class TestApplyCommand:
    async def test_stop_releases_from_supervision_before_stopping(self) -> None:
        actor = _Recorder()
        actor._registry = _Registry(actor)

        assert await actor.apply_command("stop") is True

        # Without the release, the heartbeat watchdog restarts the actor ~35s
        # after this deliberate stop.
        assert actor._registry._supervisor_ref.released == ["worker"]
        assert actor.calls == ["stop"]

    async def test_delete_releases_unregisters_and_stops(self) -> None:
        actor = _Recorder()
        registry = _Registry(actor)
        actor._registry = registry

        assert await actor.apply_command("delete") is True

        assert registry._supervisor_ref.released == ["worker"]
        assert registry.unregistered == [actor.actor_id]
        assert actor.calls == ["stop"]

    async def test_a_protected_actor_refuses_and_says_so(self) -> None:
        actor = _Recorder(protected=True)
        actor._registry = _Registry(actor)

        for command in ("stop", "pause", "delete"):
            assert await actor.apply_command(command) is False

        assert actor.calls == []
        assert actor._registry._supervisor_ref.released == []

    async def test_a_protected_actor_still_resumes(self) -> None:
        actor = _Recorder(protected=True)

        assert await actor.apply_command("resume") is True
        assert actor.calls == ["resume"]

    async def test_an_unknown_command_is_reported_not_ignored(self) -> None:
        actor = _Recorder()

        assert await actor.apply_command("restart") is False
        assert actor.calls == []


class TestDispatchCommand:
    async def test_a_local_actor_is_driven_without_the_broker(self) -> None:
        actor = _Recorder()
        runtime.registry = _Registry(actor)
        runtime.mqtt_client_ref = None

        assert await lifecycle.dispatch_command(actor.actor_id, "pause", "test") == "local"
        assert actor.calls == ["pause"]

    async def test_a_local_actor_is_not_routed_through_the_broker(self) -> None:
        actor = _Recorder()
        runtime.registry = _Registry(actor)
        runtime.mqtt_client_ref = broker = _Broker()

        await lifecycle.dispatch_command(actor.actor_id, "pause", "test")

        assert actor.calls == ["pause"]
        assert broker.published == []

    async def test_a_remote_actor_still_goes_over_the_broker(self) -> None:
        runtime.registry = _Registry(None)
        runtime.mqtt_client_ref = broker = _Broker()

        assert await lifecycle.dispatch_command("remote-1", "pause", "test") == "broker"
        assert broker.published[0][0] == "agents/remote-1/commands"

    async def test_an_undeliverable_command_reports_failure(self) -> None:
        runtime.registry = _Registry(None)
        runtime.mqtt_client_ref = None

        assert await lifecycle.dispatch_command("remote-1", "pause", "test") == ""

    async def test_a_refusal_is_distinguished_from_a_failure(self) -> None:
        actor = _Recorder(protected=True)
        runtime.registry = _Registry(actor)
        runtime.mqtt_client_ref = None

        assert await lifecycle.dispatch_command(actor.actor_id, "pause", "test") == "refused"


class TestHTTPHandlers:
    async def test_pause_takes_effect_with_the_broker_down(self) -> None:
        actor = _Recorder()
        runtime.registry = _Registry(actor)
        runtime.mqtt_client_ref = None

        response = await api_actors.pause_actor_handler(_Request(actor.actor_id))

        assert response.status == 200
        assert _payload(response) == {"status": "pausing"}
        assert actor.calls == ["pause"]

    async def test_resume_takes_effect_with_the_broker_down(self) -> None:
        actor = _Recorder()
        runtime.registry = _Registry(actor)
        runtime.mqtt_client_ref = None

        response = await api_actors.resume_actor_handler(_Request(actor.actor_id))

        assert response.status == 200
        assert actor.calls == ["resume"]

    async def test_a_protected_actor_is_refused(self) -> None:
        actor = _Recorder(protected=True)
        runtime.registry = _Registry(actor)

        response = await api_actors.pause_actor_handler(_Request(actor.actor_id))

        assert response.status == 403
        assert actor.calls == []


class TestWebSocketCommands:
    async def test_pause_reaches_a_local_actor_with_the_broker_down(self) -> None:
        actor = _Recorder()
        runtime.registry = _Registry(actor)
        runtime.mqtt_client_ref = None
        runtime.state["agents"][actor.actor_id] = {"state": "running"}

        try:
            await ws.handle_command({"command": "pause", "agent_id": actor.actor_id})
        finally:
            runtime.state["agents"].pop(actor.actor_id, None)

        assert actor.calls == ["pause"]

    async def test_an_undelivered_command_does_not_move_the_dashboard(self) -> None:
        # The agent is on another node and the broker is down: nothing happened,
        # so the browser must not be told the agent is now paused.
        runtime.registry = _Registry(None)
        runtime.mqtt_client_ref = None
        runtime.state["agents"]["remote-1"] = {"state": "running"}

        try:
            await ws.handle_command({"command": "pause", "agent_id": "remote-1"})
            assert runtime.state["agents"]["remote-1"]["state"] == "running"
        finally:
            runtime.state["agents"].pop("remote-1", None)


class TestCommandListener:
    async def test_a_protected_actor_keeps_listening_after_refusing_stop(self) -> None:
        # The listener returns after stop/delete. If it returned on a *refused*
        # stop, a protected actor would go deaf to every later command.
        actor = _Recorder(protected=True)
        actor._registry = _Registry(actor)

        assert await actor.apply_command("stop") is False
        assert actor.state is not ActorState.STOPPED
