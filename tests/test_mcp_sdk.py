# SPDX-License-Identifier: Apache-2.0
"""Exercise the SDK's real transports and the server's public wire contract."""

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from wactorz.core.integrations import google_mcp
from wactorz.core.integrations.google_mcp import GoogleMcpClient, GoogleMcpConfig


class _WactorzBackend:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def chat(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        assert request.headers["X-API-Key"] == "test-key"
        return web.json_response({"response": "hello through MCP"})


async def test_stdio_preserves_tools_resources_and_calls() -> None:
    """The contract fixture records the SDK 1 server's client-visible definitions."""
    expected = json.loads(Path(__file__).with_name("mcp_contract.json").read_text("utf-8"))
    receiver = _WactorzBackend()
    app = web.Application()
    app.router.add_post("/chat", receiver.chat)
    async with TestServer(app) as backend:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "wactorz.interfaces.mcp_server"],
            env={"WACTORZ_URL": str(backend.make_url("")), "WACTORZ_API_KEY": "test-key"},
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            tools = await client.list_tools()
            resources = await client.list_resources()
            assert [
                t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in tools.tools
            ] == expected["tools"]
            assert [
                r.model_dump(mode="json", by_alias=True, exclude_none=True)
                for r in resources.resources
            ] == expected["resources"]
            result = await client.call_tool("ask_agent", {"agent_name": "weather", "message": "hi"})
            assert not result.is_error
            assert result.content[0].type == "text"
            assert result.content[0].text == "hello through MCP"
            invalid = await client.call_tool("ask_agent", {"message": "hi"})
            assert invalid.is_error
            unknown = await client.call_tool("missing_tool", {})
            assert unknown.is_error
    assert receiver.requests == [{"message": "hi", "agent_name": "weather"}]


class _HostedMcp:
    """A local hosted endpoint using MCP's camelCase JSON wire fields."""

    def __init__(self) -> None:
        self.authorization: list[str | None] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.authorization.append(request.headers.get("Authorization"))
        if request.method != "POST":
            return web.Response(status=405)
        message = await request.json()
        if "id" not in message:
            return web.Response(status=202)
        method = message["method"]
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "local-google", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "list_events",
                        "description": "Lists events",
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        else:
            assert method == "tools/call"
            assert message["params"]["name"] == "list_events"
            result = {
                "content": [{"type": "text", "text": "lossy summary"}],
                "structuredContent": {"events": [{"id": "e1"}]},
                "isError": False,
            }
        return web.json_response({"jsonrpc": "2.0", "id": message["id"], "result": result})


@pytest.fixture(name="hosted")
async def hosted_fixture() -> AsyncIterator[tuple[_HostedMcp, str]]:
    endpoint = _HostedMcp()
    app = web.Application()
    app.router.add_route("*", "/mcp", endpoint.handle)
    async with TestServer(app) as server:
        yield endpoint, str(server.make_url("/mcp"))


@pytest.mark.parametrize("oauth", [False, True])
async def test_google_client_uses_sdk_http_and_auth(
    hosted: tuple[_HostedMcp, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth: bool
) -> None:
    endpoint, url = hosted
    token_file = tmp_path / "tokens.json"
    config = GoogleMcpConfig(
        "SDK_TEST", "Test", url, "https://unused.invalid", "read", "unused.json", "list_events"
    )
    monkeypatch.setenv("SDK_TEST_TOKEN_FILE", str(token_file))
    if oauth:
        monkeypatch.setenv("SDK_TEST_CLIENT_ID", "test-client")
        monkeypatch.setenv("SDK_TEST_CLIENT_SECRET", "test-secret")
        token_file.write_text(
            json.dumps({"tokens": {"access_token": "stored-token", "token_type": "Bearer"}}),
            encoding="utf-8",
        )
    else:
        monkeypatch.setenv("SDK_TEST_TOKEN", "static-token")
    assert google_mcp.ClientSession is not None
    client = GoogleMcpClient(config)
    assert await client.list_tools() == "list_events: Lists events"
    assert json.loads(await client.call_tool("list_events", {"pageSize": 1})) == {
        "events": [{"id": "e1"}]
    }
    assert endpoint.authorization
    assert set(endpoint.authorization) == {
        "Bearer stored-token" if oauth else "Bearer static-token"
    }
