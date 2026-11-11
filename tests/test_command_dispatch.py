# SPDX-License-Identifier: Apache-2.0
"""A command from the dashboard must reach the actor, or say that it did not.

Local actors are driven in process. The web layer used to publish every command
to the broker so an actor it already held could receive its own command back over
the network — which meant that with the broker down, a command did nothing while
the response still said ``stopping`` and the dashboard kept showing the agent as
running. Nothing corrected that until the agent's next heartbeat.

The other half is the supervision release. Stopping an actor without releasing it
is indistinguishable from crashing, so the heartbeat watchdog restarts it moments
later — the delete path reached that state by calling ``stop()`` directly.
"""

import json
from collections.abc import Iterator
from typing import Any, cast

import pytest
from aiohttp import web

from wactorz.agents.main.spawn_registry import SpawnRegistry
from wactorz.agents.main.stop_start import Outcome
from wactorz.core.actor import Actor, ActorState, Message, MessageType
from wactorz.core.registry import ActorRegistry
from wactorz.core.topics import agent_topic
from wactorz.web import api_actors, lifecycle, runtime, ws


class _Recorder(Actor):
    """An actor that records lifecycle calls instead of performing them."""

    def __init__(
        self,
        name: str = "worker",
        protected: bool = False,
        essential: bool = False,
        refuses: bool = False,
    ) -> None:
        super().__init__(name=name)
        self.protected = protected
        self.essential = essential
        self.calls: list[str] = []
        self._refuses = refuses

    async def apply_command(self, command: str) -> bool:
        """Decline everything, for the tests about a command that does not take."""
        if self._refuses:
            return False
        return await super().apply_command(command)

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")

    async def handle_message(self, message) -> None:  # pragma: no cover - never delivered
        return None


class _Supervisor:
    def __init__(self) -> None:
        self.released: list[str] = []
        self.forgotten: list[str] = []
        self.resupervised: list[str] = []

    def release(self, name: str) -> None:
        self.released.append(name)

    def drop_supervised(self, name: str) -> None:
        self.forgotten.append(name)

    def resupervise(self, name: str, _actor: Actor) -> None:
        self.resupervised.append(name)


class _Registry:
    def __init__(self, actor: Actor | None = None) -> None:
        self._actor = actor
        self.supervisor = _Supervisor()
        self._supervisor_ref = self.supervisor
        self.unregistered: list[str] = []

    def get(self, actor_id: str) -> Actor | None:
        if self._actor is not None and self._actor.actor_id == actor_id:
            return self._actor
        return None

    def find_by_name(self, name: str) -> Actor | None:
        if self._actor is not None and self._actor.name == name:
            return self._actor
        return None

    def all_actors(self) -> list[Actor]:
        """Every actor this registry holds.

        Needed because a lifecycle command now broadcasts the snapshot it
        produced, and building a snapshot walks the registry — the double has to
        answer the same questions the real one does.
        """
        return [self._actor] if self._actor is not None else []

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)


def _attach(actor: Actor, registry: "_Registry") -> "_Registry":
    """Give an actor a stand-in registry, keeping the concrete type for asserts."""
    actor._registry = cast(ActorRegistry, registry)
    return registry


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self.qos: list[int] = []

    async def publish(self, topic: str, payload: str, qos: int = 0, **_kwargs: Any) -> None:
        self.published.append((topic, payload))
        self.qos.append(qos)


class _Request:
    """Just enough of ``web.Request`` for the handlers under test."""

    def __init__(self, actor_id: str) -> None:
        self.match_info = {"actor_id": actor_id}


def _request(actor_id: str) -> web.Request:
    """A stand-in request, cast at the one place the handlers need the real type."""
    return cast(web.Request, _Request(actor_id))


@pytest.fixture(autouse=True)
def _restore_runtime() -> "Iterator[None]":
    registry, client = runtime.registry, runtime.mqtt_client_ref
    yield
    runtime.registry, runtime.mqtt_client_ref = registry, client


def _payload(response) -> dict[str, Any]:
    return json.loads(response.body.decode())


class TestApplyCommand:
    async def test_stop_releases_from_supervision_before_stopping(self) -> None:
        actor = _Recorder()
        registry = _attach(actor, _Registry(actor))

        assert await actor.apply_command("stop") is True

        # Without the release, the heartbeat watchdog restarts the actor ~35s
        # after this deliberate stop.
        assert registry.supervisor.released == ["worker"]
        assert actor.calls == ["stop"]

    async def test_delete_forgets_unregisters_and_stops(self) -> None:
        # Forgotten rather than released: a deleted actor does not come back,
        # and a released entry would keep its factory for as long as the
        # process runs.
        actor = _Recorder()
        registry = _attach(actor, _Registry(actor))

        assert await actor.apply_command("delete") is True

        assert registry.supervisor.forgotten == ["worker"]
        assert registry.supervisor.released == []
        assert registry.unregistered == [actor.actor_id]
        assert actor.calls == ["stop"]

    async def test_protection_alone_does_not_refuse_a_stop(self) -> None:
        actor = _Recorder(protected=True)
        _attach(actor, _Registry(actor))

        # The whole point of the split: protection is about not losing an agent
        # that cannot be recreated, and a stop loses nothing. An actor that is
        # protected and not essential must be stoppable, or the dashboard offers
        # a button the API refuses.
        assert await actor.apply_command("stop") is True
        assert actor.calls == ["stop"]

    async def test_a_protected_actor_refuses_delete_and_says_so(self) -> None:
        actor = _Recorder(protected=True, essential=True)
        registry = _attach(actor, _Registry(actor))

        for command in ("stop", "delete"):
            assert await actor.apply_command(command) is False

        assert actor.calls == []
        assert registry.supervisor.released == []

    async def test_a_protected_actor_can_still_be_started(self) -> None:
        actor = _Recorder(protected=True)
        actor.state = ActorState.STOPPED

        # Protection is about not losing an agent, so it covers delete alone
        # and not the verbs that stop or restart one.
        assert await actor.apply_command("start") is True
        assert actor.calls == ["start"]

    async def test_an_unknown_command_is_reported_not_ignored(self) -> None:
        actor = _Recorder()

        assert await actor.apply_command("restart") is False
        assert actor.calls == []


class TestCommandDelivery:
    """A command to a remote agent is at-least-once.

    It goes out on the monitor's own client, which applies none of
    MQTTPublisher's topic classification, so the QoS has to be asked for. At
    QoS 0 a stop issued while the agent was reconnecting was simply gone -- the
    dashboard reported it stopped and it kept running.
    """

    async def test_it_is_published_at_least_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        broker = _Broker()
        monkeypatch.setattr(lifecycle.runtime, "mqtt_client_ref", broker)

        await lifecycle.dispatch_command("remote-1", "stop", sender="test")

        assert broker.qos == [1]


class TestDispatchCommand:
    async def test_a_local_actor_is_driven_without_the_broker(self) -> None:
        actor = _Recorder()
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None

        assert (await lifecycle.dispatch_command(actor.actor_id, "stop", "test")).route == "local"
        assert actor.calls == ["stop"]

    async def test_a_local_actor_is_not_routed_through_the_broker(self) -> None:
        actor = _Recorder()
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = broker = _Broker()

        await lifecycle.dispatch_command(actor.actor_id, "stop", "test")

        assert actor.calls == ["stop"]
        assert broker.published == []

    async def test_a_remote_actor_still_goes_over_the_broker(self) -> None:
        runtime.registry = cast(ActorRegistry, _Registry(None))
        runtime.mqtt_client_ref = broker = _Broker()

        assert (await lifecycle.dispatch_command("remote-1", "stop", "test")).route == "broker"
        assert broker.published[0][0] == "agents/remote-1/commands"

    async def test_an_undeliverable_command_reports_failure(self) -> None:
        runtime.registry = cast(ActorRegistry, _Registry(None))
        runtime.mqtt_client_ref = None

        assert (await lifecycle.dispatch_command("remote-1", "stop", "test")).route == ""

    async def test_a_refusal_is_distinguished_from_a_failure(self) -> None:
        actor = _Recorder(essential=True)
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None

        assert (await lifecycle.dispatch_command(actor.actor_id, "stop", "test")).route == "refused"


class TestStart:
    async def test_a_stopped_actor_can_be_started_again(self) -> None:
        actor = _Recorder()
        _registry = _attach(actor, _Registry(actor))
        await actor.apply_command("stop")
        actor.state = ActorState.STOPPED

        assert await actor.apply_command("start") is True
        assert actor.calls == ["stop", "start"]

    async def test_starting_puts_it_back_under_supervision(self) -> None:
        actor = _Recorder()
        registry = _attach(actor, _Registry(actor))
        await actor.apply_command("stop")
        actor.state = ActorState.STOPPED
        assert registry.supervisor.released == ["worker"]

        await actor.apply_command("start")

        # Stopping retires the spec so the watchdog leaves it alone. Starting
        # has to undo that, or the actor runs unwatched from then on.
        assert registry.supervisor.resupervised == ["worker"]

    async def test_starting_a_running_actor_is_refused(self) -> None:
        actor = _Recorder()
        actor.state = ActorState.RUNNING

        # Starting twice would add a second message loop and a second command
        # listener alongside the first.
        assert await actor.apply_command("start") is False
        assert actor.calls == []


class TestLifecycleMessages:
    """Message-passing is another route to the same rules, not another set."""

    async def test_a_stop_message_releases_from_supervision(self) -> None:
        actor = _Recorder()
        registry = _attach(actor, _Registry(actor))

        await actor._dispatch(Message(type=MessageType.STOP, sender_id="peer"))

        assert registry.supervisor.released == ["worker"]
        assert actor.calls == ["stop"]

    async def test_a_start_message_starts_a_stopped_actor(self) -> None:
        actor = _Recorder()
        _attach(actor, _Registry(actor))
        actor.state = ActorState.STOPPED

        # MessageType.START existed but had no handler, so it fell through to
        # the agent's own handle_message and did nothing.
        await actor._dispatch(Message(type=MessageType.START, sender_id="peer"))

        assert actor.calls == ["start"]

    async def test_an_essential_actor_refuses_a_stop_message(self) -> None:
        actor = _Recorder(essential=True)
        registry = _attach(actor, _Registry(actor))

        await actor._dispatch(Message(type=MessageType.STOP, sender_id="peer"))

        assert actor.calls == []
        assert registry.supervisor.released == []


class TestHTTPHandlers:
    async def test_stop_takes_effect_with_the_broker_down(self) -> None:
        actor = _Recorder()
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None

        response = await api_actors.stop_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        assert response.status == 200
        assert _payload(response) == {"status": "stopping"}
        assert actor.calls == ["stop"]

    async def test_start_takes_effect_with_the_broker_down(self) -> None:
        actor = _Recorder()
        # start applies only to a stopped actor; anything else is refused.
        actor.state = ActorState.STOPPED
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None

        response = await api_actors.start_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        assert response.status == 200
        assert actor.calls == ["start"]

    async def test_start_reaches_a_stopped_actor(self) -> None:
        actor = _Recorder()
        actor.state = ActorState.STOPPED
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None

        response = await api_actors.start_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        assert response.status == 200
        assert _payload(response) == {"status": "starting"}
        assert actor.calls == ["start"]

    async def test_stop_reaches_a_running_actor(self) -> None:
        """The verb that was socket-only until now, on the same local path."""
        actor = _Recorder()
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None

        response = await api_actors.stop_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        assert response.status == 200
        assert _payload(response) == {"status": "stopping"}
        assert actor.calls == ["stop"]

    async def test_an_essential_actor_cannot_be_stopped(self) -> None:
        """Stop is the command the guard exists for, so it gets its own refusal test."""
        actor = _Recorder(essential=True)
        runtime.registry = cast(ActorRegistry, _Registry(actor))

        response = await api_actors.stop_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        assert response.status == 403
        assert actor.calls == []

    async def test_a_stop_is_reflected_in_the_state_the_api_reports(self) -> None:
        """The half that was missing, and the reason the whole path is shared now.

        State reaches this dict over MQTT, so with the broker down nothing else
        was ever going to correct it: the command ran, the response said 200, and
        `GET /actors` went on reporting the agent as running indefinitely.
        """
        actor = _Recorder()
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None
        runtime.state["agents"][actor.actor_id] = {"name": actor.name, "state": "running"}

        response = await api_actors.stop_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        assert response.status == 200
        assert runtime.state["agents"][actor.actor_id]["state"] == "stopped"

    async def test_a_refused_command_is_not_reflected(self) -> None:
        """Showing a state the actor never entered is worse than showing a stale one."""
        actor = _Recorder(refuses=True)
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None
        runtime.state["agents"][actor.actor_id] = {"name": actor.name, "state": "running"}

        response = await api_actors.stop_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        assert response.status == 409
        assert runtime.state["agents"][actor.actor_id]["state"] == "running"

    async def test_a_remote_agent_is_commanded_over_the_broker(self) -> None:
        """An agent on a node is absent from the local registry by design.

        Before this it was indistinguishable from a typo and answered 404, while
        the same command from the dashboard reached it perfectly well.
        """
        runtime.registry = cast(ActorRegistry, _Registry())
        runtime.mqtt_client_ref = broker = _Broker()
        runtime.state["agents"]["remote-1"] = {"name": "picam", "state": "running"}

        response = await api_actors.stop_actor_handler(_request("picam"))  # pyright: ignore[reportArgumentType]

        assert response.status == 200
        assert [topic for topic, _ in broker.published] == ["agents/remote-1/commands"]
        assert runtime.state["agents"]["remote-1"]["state"] == "stopped"

    async def test_a_remote_agent_with_no_broker_reports_failure(self) -> None:
        """Nowhere to send it, so the caller is told rather than reassured."""
        runtime.registry = cast(ActorRegistry, _Registry())
        runtime.mqtt_client_ref = None
        runtime.state["agents"]["remote-1"] = {"name": "picam", "state": "running"}

        response = await api_actors.stop_actor_handler(_request("picam"))  # pyright: ignore[reportArgumentType]

        assert response.status == 503
        assert runtime.state["agents"]["remote-1"]["state"] == "running"

    async def test_stopping_an_unknown_actor_is_404(self) -> None:
        runtime.registry = cast(ActorRegistry, _Registry())

        response = await api_actors.stop_actor_handler(_request("nope"))  # pyright: ignore[reportArgumentType]

        assert response.status == 404

    async def test_an_essential_actor_is_refused(self) -> None:
        actor = _Recorder(essential=True)
        runtime.registry = cast(ActorRegistry, _Registry(actor))

        response = await api_actors.stop_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        assert response.status == 403
        assert actor.calls == []

    async def test_a_protected_actor_is_stopped_over_http(self) -> None:
        actor = _Recorder(protected=True)
        runtime.registry = cast(ActorRegistry, _Registry(actor))

        response = await api_actors.stop_actor_handler(_request(actor.actor_id))  # pyright: ignore[reportArgumentType]

        # The transport must not refuse what the actor would have allowed; that
        # disagreement is what let the dashboard stop an agent the API then
        # would not start again.
        assert response.status == 200
        assert actor.calls == ["stop"]


class TestWebSocketCommands:
    async def test_a_command_reaches_a_local_actor_with_the_broker_down(self) -> None:
        actor = _Recorder()
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None
        runtime.state["agents"][actor.actor_id] = {"state": "running"}

        try:
            await ws.handle_command({"command": "stop", "agent_id": actor.actor_id})
        finally:
            runtime.state["agents"].pop(actor.actor_id, None)

        assert actor.calls == ["stop"]

    async def test_an_undelivered_command_does_not_move_the_dashboard(self) -> None:
        # The agent is on another node and the broker is down: nothing happened,
        # so the browser must not be told the command took effect.
        runtime.registry = cast(ActorRegistry, _Registry(None))
        runtime.mqtt_client_ref = None
        runtime.state["agents"]["remote-1"] = {"state": "running"}

        try:
            await ws.handle_command({"command": "stop", "agent_id": "remote-1"})
            assert runtime.state["agents"]["remote-1"]["state"] == "running"
        finally:
            runtime.state["agents"].pop("remote-1", None)

    async def test_a_command_for_an_untracked_agent_creates_no_phantom_entry(self) -> None:
        # The write used to be `state["agents"].get(agent_id, {})["state"] = …`,
        # which mutated a *fresh* dict when the agent was absent — a line that
        # reads like a state update and silently was not one. An absent agent is
        # not an error: the command succeeded, and the next heartbeat re-creates
        # the entry.
        actor = _Recorder()
        runtime.registry = cast(ActorRegistry, _Registry(actor))
        runtime.mqtt_client_ref = None
        runtime.state["agents"].pop(actor.actor_id, None)

        await ws.handle_command({"command": "stop", "agent_id": actor.actor_id})

        assert actor.calls == ["stop"]  # the command still reached the actor
        assert actor.actor_id not in runtime.state["agents"]


class TestCommandListener:
    async def test_an_essential_actor_keeps_listening_after_refusing_stop(self) -> None:
        # The listener returns after stop/delete. If it returned on a *refused*
        # stop, an essential actor would go deaf to every later command.
        actor = _Recorder(essential=True)
        _registry = _attach(actor, _Registry(actor))

        assert await actor.apply_command("stop") is False
        assert actor.state != ActorState.STOPPED


class _MainWithNodes:
    """Main, as far as stopping and starting an agent on a node goes."""

    def __init__(self, entries: dict[str, dict[str, Any]], answer: Outcome) -> None:
        self._entries = entries
        self.answer = answer
        self.asked: list[tuple[str, str, str, str | None]] = []

    def _get_spawn_registry(self) -> SpawnRegistry:
        return SpawnRegistry(self._entries)

    async def stop_agent_on_node(
        self, name: str, node: str, actor_id: str | None = None
    ) -> Outcome:
        self.asked.append(("stop", name, node, actor_id))
        return self.answer

    async def start_agent_on_node(
        self, name: str, node: str, actor_id: str | None = None
    ) -> Outcome:
        self.asked.append(("start", name, node, actor_id))
        return self.answer


class TestAnAgentOnANode:
    """It is stopped and started through main: its own commands topic is not a way to it."""

    @pytest.fixture(name="broker")
    def broker_fixture(self) -> Iterator[_Broker]:
        saved = dict(runtime.state["agents"])
        runtime.state["agents"].clear()
        runtime.registry = cast(ActorRegistry, _Registry(None))
        runtime.mqtt_client_ref = broker = _Broker()
        yield broker
        runtime.state["agents"].clear()
        runtime.state["agents"].update(saved)

    def _main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        answer: Outcome,
        entries: dict[str, dict[str, Any]] | None = None,
    ) -> _MainWithNodes:
        main = _MainWithNodes(entries or {}, answer)
        monkeypatch.setattr(lifecycle, "find_main_actor", lambda _registry: main)
        return main

    @pytest.mark.parametrize("command", ["stop", "start"])
    async def test_one_main_keeps_is_found_by_its_id(
        self, broker: _Broker, monkeypatch: pytest.MonkeyPatch, command: str
    ) -> None:
        # Two agents of a name: the id says which, where a name could not.
        entries = {
            "id-pi": {"name": "cam", "node": "rpi", "actor_id": "id-pi"},
            "id-pc": {"name": "cam", "node": "pc", "actor_id": "id-pc"},
        }
        main = self._main(monkeypatch, Outcome(True, "sent"), entries)

        routed = await lifecycle.dispatch_command("id-pc", command, "test")

        assert (routed.route, routed.detail, routed.done) == ("node", "sent", True)
        assert main.asked == [(command, "cam", "pc", "id-pc")]
        assert broker.published == []

    async def test_one_main_did_not_spawn_is_found_by_what_it_reports(
        self, broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        main = self._main(monkeypatch, Outcome(True, "sent"))
        runtime.state["agents"]["r1"] = {"name": "its-own", "node": "rpi"}

        routed = await lifecycle.dispatch_command("r1", "stop", "test")

        assert routed.route == "node"
        assert main.asked == [("stop", "its-own", "rpi", "r1")]
        assert broker.published == []

    async def test_what_main_refuses_is_refused_with_its_reason(
        self, broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        why = "'cam' was not started: node 'rpi' is not online. It stays stopped."
        entries = {"id-pi": {"name": "cam", "node": "rpi", "actor_id": "id-pi"}}
        self._main(monkeypatch, Outcome(False, why), entries)

        routed = await lifecycle.dispatch_command("id-pi", "start", "test")

        assert (routed.route, routed.detail, routed.done) == ("refused", why, False)

    async def test_without_main_it_is_not_delivered_and_says_so(
        self, broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(lifecycle, "find_main_actor", lambda _registry: None)
        runtime.state["agents"]["r1"] = {"name": "cam", "node": "rpi"}

        routed = await lifecycle.dispatch_command("r1", "stop", "test")

        assert routed.route == ""
        assert "through main, and main is not running in this process" in routed.detail
        # Not sent where the agent does not listen, to be reported as delivered.
        assert broker.published == []

    @pytest.mark.parametrize("node", ["", "main", "local"])
    async def test_an_agent_on_no_node_is_sent_the_command_itself(
        self, broker: _Broker, monkeypatch: pytest.MonkeyPatch, node: str
    ) -> None:
        main = self._main(monkeypatch, Outcome(True, "sent"))
        runtime.state["agents"]["r1"] = {"name": "apart", "node": node}

        routed = await lifecycle.dispatch_command("r1", "stop", "test")

        assert routed.route == "broker"
        assert main.asked == []
        assert broker.published[0][0] == agent_topic("r1", "commands")

    async def test_a_command_main_carried_out_is_shown_to_every_dashboard(
        self, broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = {"id-pi": {"name": "cam", "node": "rpi", "actor_id": "id-pi"}}
        self._main(monkeypatch, Outcome(True, "sent"), entries)
        runtime.state["agents"]["id-pi"] = {"name": "cam", "node": "rpi", "state": "running"}
        frames: list[dict[str, Any]] = []

        async def _broadcast(frame: dict[str, Any]) -> None:
            frames.append(frame)

        monkeypatch.setattr(ws, "broadcast", _broadcast)

        routed = await lifecycle.run_command("id-pi", "stop", "test")

        assert routed.done
        assert runtime.state["agents"]["id-pi"]["state"] == "stopped"
        assert [frame["type"] for frame in frames] == ["patch"]

    async def test_one_main_refused_leaves_every_dashboard_as_it_was(
        self, broker: _Broker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entries = {"id-pi": {"name": "cam", "node": "rpi", "actor_id": "id-pi"}}
        self._main(monkeypatch, Outcome(False, "'cam' on node 'rpi' is not stopped."), entries)
        runtime.state["agents"]["id-pi"] = {"name": "cam", "node": "rpi", "state": "running"}

        routed = await lifecycle.run_command("id-pi", "start", "test")

        assert not routed.done
        assert runtime.state["agents"]["id-pi"]["state"] == "running"
