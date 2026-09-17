"""The MCP server's tools against real HTTP endpoints standing in for Wactorz and HA.

Each tool is a thin translation from an MCP call to one HTTP request, so what
matters is the translation and what a failure reads like to the client: a
server that is down is named, an error status is shown with its body, and a
malformed argument is refused before any request is made.

Served by an in-process aiohttp application on a random local port, so the
requests are real and nothing leaves the machine.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("mcp.server.fastmcp")

from aiohttp import web
from aiohttp.test_utils import TestServer

from wactorz.interfaces import mcp_server


class _Backend:
    """One app answering both the Wactorz REST API and the Home Assistant API."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, Any]] = []
        self.chat_reply: Any = {"response": "hello from main"}
        self.agents: Any = [
            {"id": "abc123", "name": "weather", "state": "running", "protected": True}
        ]
        self.states = [
            {"entity_id": "sensor.t", "state": "21", "attributes": {"friendly_name": "Temp"}},
            {"entity_id": "light.hall", "state": "on", "attributes": {}},
        ]
        self.fail = False

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/chat", self._chat)
        app.router.add_get("/agents", self._agents)
        app.router.add_delete("/actors/{id}", self._delete)
        app.router.add_get("/ha-map", self._ha_map)
        app.router.add_get("/api/states", self._states)
        app.router.add_get("/api/states/{eid}", self._state)
        app.router.add_post("/api/services/{domain}/{service}", self._service)
        return app

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(("POST", "/chat", await request.json()))
        if isinstance(self.chat_reply, str):
            return web.Response(text=self.chat_reply, status=502)
        return web.json_response(self.chat_reply)

    async def _agents(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(("GET", "/agents", dict(request.headers)))
        if isinstance(self.agents, str):
            return web.Response(text=self.agents)
        return web.json_response(self.agents)

    async def _delete(self, request: web.Request) -> web.StreamResponse:
        agent_id = request.match_info["id"]
        return web.Response(
            text="gone" if agent_id == "abc123" else "unknown",
            status=200 if agent_id == "abc123" else 404,
        )

    async def _ha_map(self, request: web.Request) -> web.StreamResponse:
        return web.Response(text="not ready")

    async def _states(self, request: web.Request) -> web.StreamResponse:
        if self.fail:
            return web.Response(text="unauthorised", status=401)
        return web.json_response(self.states)

    async def _state(self, request: web.Request) -> web.StreamResponse:
        eid = request.match_info["eid"]
        if eid == "sensor.broken":
            return web.Response(text="boom", status=500)
        match = next((s for s in self.states if s["entity_id"] == eid), None)
        return web.json_response(match) if match else web.Response(status=404)

    async def _service(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(("POST", request.path, body))
        if request.match_info["service"] == "explode":
            return web.Response(text="service not found", status=400)
        return web.json_response([])


@pytest.fixture(name="backend")
async def backend_fixture(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Backend]:
    backend = _Backend()
    server = TestServer(backend.app())
    await server.start_server()
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setattr(mcp_server, "WACTORZ_URL", base)
    monkeypatch.setattr(mcp_server, "WACTORZ_API_KEY", "k3y")
    monkeypatch.setattr(mcp_server, "HA_URL", base)
    monkeypatch.setattr(mcp_server, "HA_TOKEN", "t0ken")
    yield backend
    await server.close()


@pytest.fixture(name="nowhere")
def nowhere_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every URL at a port nothing listens on."""
    dead = "http://127.0.0.1:9"
    monkeypatch.setattr(mcp_server, "WACTORZ_URL", dead)
    monkeypatch.setattr(mcp_server, "HA_URL", dead)
    monkeypatch.setattr(mcp_server, "HA_TOKEN", "t0ken")


class TestOrchestrator:
    async def test_messages_reach_main_or_a_named_agent(self, backend: _Backend) -> None:
        assert await mcp_server.ask_wactorz("hi") == "hello from main"
        await mcp_server.ask_agent("weather", "rain?")
        await mcp_server.list_capabilities("pdf")

        assert [body for _, path, body in backend.requests if path == "/chat"] == [
            {"message": "hi"},
            {"message": "rain?", "agent_name": "weather"},
            {"message": "/capabilities pdf"},
        ]

    async def test_a_reply_that_is_not_json_is_shown_with_its_status(
        self, backend: _Backend
    ) -> None:
        backend.chat_reply = "Bad Gateway"

        assert await mcp_server.ask_wactorz("hi") == "{'status': 502, 'text': 'Bad Gateway'}"

    async def test_agents_are_listed_with_the_api_key(self, backend: _Backend) -> None:
        listed = await mcp_server.list_agents()

        assert listed == "[running   ] @weather [protected]  (id: abc123)"
        assert backend.requests[0][2]["X-API-Key"] == "k3y"

    @pytest.mark.parametrize(
        ("agents", "reply"), [([], "No agents running."), ("plain text", "No agents running.")]
    )
    async def test_no_agents_says_so(self, backend: _Backend, agents: Any, reply: str) -> None:
        backend.agents = agents

        assert await mcp_server.list_agents() == reply

    async def test_stopping_an_agent_reports_the_outcome(self, backend: _Backend) -> None:
        assert await mcp_server.stop_agent("abc123") == "Agent abc123 stopped."
        assert await mcp_server.stop_agent("zzz") == "Error 404: unknown"

    async def test_resources(self, backend: _Backend) -> None:
        assert json.loads(await mcp_server.agents_resource())[0]["name"] == "weather"
        assert await mcp_server.capabilities_resource() == "hello from main"
        assert await mcp_server.ha_map_resource() == "not ready"

    async def test_a_wactorz_that_is_down_is_named(self, nowhere: None) -> None:
        assert "Cannot connect to wactorz" in await mcp_server.ask_wactorz("hi")
        assert "Cannot connect to wactorz" in await mcp_server.list_agents()
        assert "Cannot connect to wactorz" in await mcp_server.stop_agent("x")

    async def test_an_unexpected_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server, "WACTORZ_URL", "not a url")

        assert "error" in await mcp_server._wactorz_post("/chat", {})
        assert "error" in await mcp_server._wactorz_get("/agents")
        assert (await mcp_server._wactorz_delete("/actors/x"))["status"] == 0


class TestHomeAssistant:
    async def test_entities_are_listed_sorted_and_filtered(self, backend: _Backend) -> None:
        everything = await mcp_server.ha_list_entities()
        lights = await mcp_server.ha_list_entities("light")

        assert everything.splitlines()[0].startswith("light.hall")
        assert "Temp" in everything
        assert lights.count("\n") == 0
        assert (
            await mcp_server.ha_list_entities("climate")
            == "No entities found for domain 'climate'."
        )

    async def test_a_refused_listing_shows_the_status(self, backend: _Backend) -> None:
        backend.fail = True

        assert await mcp_server.ha_list_entities() == "HA error 401: unauthorised"

    async def test_one_state_found_missing_or_failing(self, backend: _Backend) -> None:
        assert json.loads(await mcp_server.ha_get_state("sensor.t"))["state"] == "21"
        assert await mcp_server.ha_get_state("sensor.nope") == "Entity 'sensor.nope' not found."
        assert await mcp_server.ha_get_state("sensor.broken") == "HA error 500: boom"

    async def test_a_service_call_carries_the_entity_and_data(self, backend: _Backend) -> None:
        reply = await mcp_server.ha_call_service(
            "light", "turn_on", "light.hall", '{"brightness": 128}'
        )

        assert reply == "OK — light.turn_on called on light.hall"
        assert backend.requests[-1] == (
            "POST",
            "/api/services/light/turn_on",
            {"brightness": 128, "entity_id": "light.hall"},
        )
        assert (
            await mcp_server.ha_call_service("light", "explode")
            == "HA error 400: service not found"
        )

    @pytest.mark.parametrize(
        ("data", "error"),
        [("{broken", "Invalid data_json"), ("[1]", "data_json must encode a JSON object.")],
    )
    async def test_malformed_service_data_is_refused_before_calling(
        self, backend: _Backend, data: str, error: str
    ) -> None:
        assert (await mcp_server.ha_call_service("light", "turn_on", data_json=data)).startswith(
            error
        )
        assert backend.requests == []

    async def test_a_home_assistant_that_is_down_is_named(self, nowhere: None) -> None:
        assert "Cannot connect to Home Assistant" in await mcp_server.ha_list_entities()
        assert "Cannot connect to Home Assistant" in await mcp_server.ha_get_state("x")
        assert "Cannot connect to Home Assistant" in await mcp_server.ha_call_service("a", "b")

    async def test_an_unexpected_failure_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mcp_server, "HA_URL", "not a url")
        monkeypatch.setattr(mcp_server, "HA_TOKEN", "t")

        assert (await mcp_server.ha_list_entities()).startswith("HA request failed")
        assert (await mcp_server.ha_get_state("x")).startswith("HA request failed")
        assert (await mcp_server.ha_call_service("a", "b")).startswith("HA request failed")


class TestCalendarTools:
    @pytest.fixture(autouse=True)
    def _calendar(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
        calls: list[tuple[str, Any]] = []

        async def _call(tool: str, arguments: dict[str, Any] | None = None) -> str:
            calls.append((tool, arguments))
            return f"called {tool}"

        class _Client:
            async def list_tools(self) -> str:
                return "list_events: Lists events"

            async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> str:
                return await _call(tool, arguments)

        monkeypatch.setattr(mcp_server, "GoogleCalendarMcpClient", _Client)
        monkeypatch.setenv("CALENDAR_MCP_TIMEZONE", "UTC")
        self.calls = calls
        return calls

    async def test_each_tool_calls_the_calendar(self) -> None:
        assert await mcp_server.calendar_mcp_list_tools() == "list_events: Lists events"
        await mcp_server.calendar_today()
        await mcp_server.calendar_week()
        await mcp_server.calendar_delete_event("e1")
        await mcp_server.calendar_create_event("Trip", "s", "e", location="Oslo", description="d")
        await mcp_server.calendar_mcp_call_tool("list_calendars", '{"a": 1}')

        tools = [tool for tool, _ in self.calls]
        assert tools == [
            "list_events",
            "list_events",
            "delete_event",
            "create_event",
            "list_calendars",
        ]
        assert self.calls[0][1]["timeZone"] == "UTC"
        assert self.calls[3][1] == {
            "summary": "Trip",
            "startTime": "s",
            "endTime": "e",
            "location": "Oslo",
            "description": "d",
        }

    async def test_bad_json_arguments_are_refused(self) -> None:
        assert (await mcp_server.calendar_mcp_call_tool("x", "{nope")).startswith(
            "Invalid arguments_json"
        )
        assert self.calls == []

    def test_other_ranges_and_an_unknown_zone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CALENDAR_MCP_TIMEZONE", "Nowhere/Land")

        args = mcp_server._calendar_range_arguments("tomorrow", count=3)

        assert args["pageSize"] == 3

    def test_main_runs_the_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ran: list[bool] = []
        monkeypatch.setattr(mcp_server.mcp, "run", lambda: ran.append(True))

        mcp_server.main()

        assert ran == [True]
