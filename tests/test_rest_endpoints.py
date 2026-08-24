"""Behaviour of the REST routes, with no key configured.

Auth is covered separately; these exercise what each route does once a request
is allowed through — including the shapes of body that should be refused
rather than raise. Most of the file drives the REST interface; the last class
covers the monitor app, whose lifecycle verbs are registered under both the
`/api/` prefix and the bare spelling.
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from wactorz.interfaces.chat.rest import RESTInterface
from wactorz.web import runtime as monitor_runtime
from wactorz.web.app import build_app as build_monitor_app

# Endpoints that read named fields off a JSON body.
BODY_ENDPOINTS = ["/chat", "/agents/command", "/actors/a1/message"]


class _Metrics:
    messages_received = 9
    messages_processed = 7
    errors = 2
    heartbeats = 4
    last_heartbeat = 123.7
    restart_count = 1


class _Actor:
    def __init__(self, name: str = "worker", protected: bool = False) -> None:
        self.actor_id = "a1"
        self.name = name
        self.protected = protected
        self.metrics = _Metrics()
        self.commands: list[str] = []
        self.refuse = False

    def get_status(self) -> dict[str, str]:
        return {"state": "idle"}

    async def apply_command(self, command: str) -> bool:
        self.commands.append(command)
        return not self.refuse


class _MapActor:
    def get_latest_map_payload(self) -> dict[str, Any]:
        return {"type": "home_assistant_map_update", "devices": []}


class _Registry:
    def __init__(self, actor: _Actor | None = None, map_actor: Any = None) -> None:
        self.actor = actor
        self.map_actor = map_actor
        self.unregistered: list[str] = []

    def get(self, actor_id: str) -> _Actor | None:
        return self.actor if self.actor and actor_id == self.actor.actor_id else None

    def all_actors(self) -> list[_Actor]:
        return [self.actor] if self.actor else []

    def find_by_name(self, name: str) -> Any:
        return self.map_actor if name == "home-assistant-map-agent" else None

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)


class _Main:
    def __init__(self, registry: _Registry) -> None:
        self._registry = registry
        self.commands: list[tuple[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    async def process_user_input(self, message: str) -> str:
        return f"echo:{message}"

    async def send_command(self, target: str, command: Any) -> None:
        self.commands.append((target, command))

    async def send(self, actor_id: str, msg_type: Any, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _make(actor: _Actor | None = None, map_actor: Any = None) -> tuple[_Main, RESTInterface]:
    main = _Main(_Registry(actor, map_actor))
    return main, RESTInterface(main, port=0)  # pyright: ignore[reportArgumentType]


@pytest.fixture(name="actor")
def actor_fixture() -> _Actor:
    return _Actor()


@pytest.fixture(name="main_and_client")
async def main_and_client_fixture(actor: _Actor) -> AsyncGenerator[tuple[_Main, TestClient], None]:
    main, iface = _make(actor, map_actor=_MapActor())
    client = TestClient(TestServer(iface.build_app()))
    await client.start_server()
    yield main, client
    await client.close()


@pytest.fixture(name="client")
async def client_fixture(main_and_client: tuple[_Main, TestClient]) -> TestClient:
    return main_and_client[1]


class TestBodiesThatAreNotObjects:
    @pytest.mark.parametrize("path", BODY_ENDPOINTS)
    async def test_a_json_list_is_a_client_error(self, client: TestClient, path: str) -> None:
        # Reading a named field off a list has no answer; that is the caller's
        # mistake, not a server fault.
        assert (await client.post(path, json=[1, 2])).status == 400

    @pytest.mark.parametrize("path", BODY_ENDPOINTS)
    async def test_a_bare_string_is_a_client_error(self, client: TestClient, path: str) -> None:
        assert (await client.post(path, json="hello")).status == 400

    @pytest.mark.parametrize("path", BODY_ENDPOINTS)
    async def test_malformed_json_is_a_client_error(self, client: TestClient, path: str) -> None:
        resp = await client.post(
            path, data="{not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status == 400


class TestChat:
    async def test_a_message_reaches_the_agent(self, client: TestClient) -> None:
        resp = await client.post("/chat", json={"message": "hi"})
        assert resp.status == 200
        assert (await resp.json())["response"] == "echo:hi"

    async def test_an_empty_message_is_refused(self, client: TestClient) -> None:
        assert (await client.post("/chat", json={"message": ""})).status == 400

    async def test_the_agent_name_defaults_to_main(self, client: TestClient) -> None:
        resp = await client.post("/chat", json={"message": "hi"})
        assert (await resp.json())["agent"] == "main"


class TestCommands:
    async def test_a_known_command_is_forwarded(
        self, main_and_client: tuple[_Main, TestClient]
    ) -> None:
        main, client = main_and_client
        resp = await client.post("/agents/command", json={"target": "a1", "command": "pause"})
        assert resp.status == 200
        assert main.commands[0][0] == "a1"

    async def test_an_unknown_command_is_refused(self, client: TestClient) -> None:
        resp = await client.post("/agents/command", json={"target": "a1", "command": "explode"})
        assert resp.status == 400

    async def test_a_command_with_no_target_is_refused(self, client: TestClient) -> None:
        assert (await client.post("/agents/command", json={"command": "pause"})).status == 400


class TestActorRoutes:
    async def test_listing_reports_the_actor(self, client: TestClient) -> None:
        payload = await (await client.get("/actors")).json()
        assert [a["name"] for a in payload] == ["worker"]

    async def test_idle_is_reported_as_initializing(self, client: TestClient) -> None:
        payload = await (await client.get("/actors/a1")).json()
        assert payload["state"] == "initializing"

    async def test_an_unknown_actor_is_404(self, client: TestClient) -> None:
        assert (await client.get("/actors/nope")).status == 404

    async def test_a_message_is_delivered(self, main_and_client: tuple[_Main, TestClient]) -> None:
        main, client = main_and_client
        resp = await client.post("/actors/a1/message", json={"content": "do it"})
        assert resp.status == 200
        assert main.sent[0]["text"] == "do it"

    async def test_an_empty_message_is_refused(self, client: TestClient) -> None:
        assert (await client.post("/actors/a1/message", json={"content": ""})).status == 400

    async def test_metrics_report_the_counters(self, client: TestClient) -> None:
        payload = await (await client.get("/actors/a1/metrics")).json()
        assert payload["messages_received"] == 9
        assert payload["heartbeats"] == 4


class TestLifecycle:
    @pytest.mark.parametrize(
        ("verb", "command"),
        [("start", "start"), ("stop", "stop"), ("pause", "pause"), ("resume", "resume")],
    )
    async def test_a_verb_runs_the_actors_own_command(
        self, actor: _Actor, client: TestClient, verb: str, command: str
    ) -> None:
        # Routed through apply_command so supervision is released properly,
        # rather than calling stop()/pause() behind the supervisor's back.
        resp = await client.post(f"/actors/a1/{verb}")
        assert resp.status == 200
        assert actor.commands == [command]

    async def test_stop_does_not_unregister(
        self, main_and_client: tuple[_Main, TestClient], actor: _Actor
    ) -> None:
        """Stop parks the actor for a later start; delete is the route that removes."""
        main, client = main_and_client
        resp = await client.post("/actors/a1/stop")
        assert resp.status == 200
        assert actor.commands == ["stop"]
        assert main._registry.unregistered == []

    async def test_a_protected_actor_is_refused(self, client: TestClient, actor: _Actor) -> None:
        actor.protected = True
        assert (await client.post("/actors/a1/pause")).status == 403

    async def test_a_protected_actor_cannot_be_stopped(
        self, client: TestClient, actor: _Actor
    ) -> None:
        actor.protected = True
        assert (await client.post("/actors/a1/stop")).status == 403
        assert actor.commands == []

    async def test_a_refused_command_reports_conflict(
        self, client: TestClient, actor: _Actor
    ) -> None:
        actor.refuse = True
        assert (await client.post("/actors/a1/pause")).status == 409

    async def test_delete_also_unregisters(
        self, main_and_client: tuple[_Main, TestClient], actor: _Actor
    ) -> None:
        main, client = main_and_client
        resp = await client.delete("/actors/a1")
        assert resp.status == 200
        assert actor.commands == ["stop"]
        assert main._registry.unregistered == ["a1"]

    async def test_deleting_an_unknown_actor_does_not_unregister(
        self, main_and_client: tuple[_Main, TestClient]
    ) -> None:
        main, client = main_and_client
        assert (await client.delete("/actors/nope")).status == 404
        assert main._registry.unregistered == []


class TestProbesAndSnapshots:
    async def test_health(self, client: TestClient) -> None:
        assert (await (await client.get("/health")).json())["status"] == "ok"

    async def test_the_ha_map_snapshot_is_served(self, client: TestClient) -> None:
        payload = await (await client.get("/ha-map")).json()
        assert payload["type"] == "home_assistant_map_update"

    async def test_a_missing_ha_map_snapshot_is_404(self) -> None:
        _main, iface = _make(_Actor(), map_actor=None)
        client = TestClient(TestServer(iface.build_app()))
        await client.start_server()
        try:
            assert (await client.get("/ha-map")).status == 404
        finally:
            await client.close()


class TestMonitorStopRoute:
    """The monitor's stop verb, registered under both spellings like every route."""

    @pytest.fixture(name="monitor_client")
    async def monitor_client_fixture(
        self, monkeypatch: pytest.MonkeyPatch, actor: _Actor
    ) -> AsyncGenerator[TestClient, None]:
        monkeypatch.setattr(monitor_runtime, "registry", _Registry(actor))
        client = TestClient(TestServer(build_monitor_app()))
        await client.start_server()
        yield client
        await client.close()

    @pytest.mark.parametrize("path", ["/api/actors/a1/stop", "/actors/a1/stop"])
    async def test_stop_runs_through_apply_command_on_both_spellings(
        self, monitor_client: TestClient, actor: _Actor, path: str
    ) -> None:
        resp = await monitor_client.post(path)
        assert resp.status == 200
        assert (await resp.json()) == {"status": "stopping"}
        assert actor.commands == ["stop"]

    async def test_a_protected_actor_is_refused_and_not_stopped(
        self, monitor_client: TestClient, actor: _Actor
    ) -> None:
        actor.protected = True
        resp = await monitor_client.post("/api/actors/a1/stop")
        assert resp.status == 403
        assert actor.commands == []
