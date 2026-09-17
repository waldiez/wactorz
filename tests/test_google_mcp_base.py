"""The shared Google client: hosted MCP first, the REST API when that is refused.

Google's hosted MCP servers answer tool calls with PERMISSION_DENIED for any
project not on their allowlist, so the fallback is the path most installs take.
It is chosen by reading the MCP result, which makes the two detectors the
switch: a result that looks denied or unavailable goes to REST, anything else
is returned as MCP gave it.

REST uses the OAuth token the MCP flow stored. A 401 is answered by refreshing
that token once and retrying; a refresh response that omits the refresh token
must not erase the one already stored, or every later refresh fails.
"""

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from wactorz.core.integrations import google_mcp
from wactorz.core.integrations.google_mcp import (
    GoogleMcpClient,
    GoogleMcpConfig,
    GoogleRestError,
    _GoogleTokenStorage,
    build_auth,
    format_mcp_content,
    mcp_timeout,
    rest_error_message,
)

needs_mcp = pytest.mark.skipif(
    google_mcp.OAuthToken is None, reason="the mcp extra is not installed"
)

PREFIX = "TEST_GMCP"


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> GoogleMcpConfig:
    for suffix in (
        "URL",
        "REDIRECT_URI",
        "SCOPES",
        "CLIENT_ID",
        "CLIENT_SECRET",
        "TOKEN",
        "AUTHORIZATION",
    ):
        monkeypatch.delenv(f"{PREFIX}_{suffix}", raising=False)
    monkeypatch.setenv(f"{PREFIX}_TOKEN_FILE", str(tmp_path / "tokens.json"))
    for suffix, value in env.items():
        monkeypatch.setenv(f"{PREFIX}_{suffix}", value)
    return GoogleMcpConfig(
        prefix=PREFIX,
        label="Test",
        default_mcp_url="https://testmcp.example/mcp/",
        api_base="https://api.example/v1",
        default_scopes="scope.read",
        token_filename="test.json",
        login_tool="whoami",
    )


def _write_tokens(config: GoogleMcpConfig, tokens: dict[str, Any], **extra: Any) -> None:
    Path(config.token_file()).write_text(json.dumps({"tokens": tokens, **extra}), encoding="utf-8")


def _stored(config: GoogleMcpConfig) -> dict[str, Any]:
    return json.loads(Path(config.token_file()).read_text(encoding="utf-8"))


class _Response:
    def __init__(self, status: int, body: Any = None, text: str = "") -> None:
        self.status = status
        self._body = body
        self._text = text

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _HttpSession:
    """Replaces aiohttp.ClientSession, answering from a queue of responses."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = responses
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self) -> "_HttpSession":
        return self

    async def __aenter__(self) -> "_HttpSession":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.requests.append(("POST", url, kwargs))
        return self._responses.pop(0)

    def request(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.requests.append((method, url, kwargs))
        return self._responses.pop(0)


def _http(monkeypatch: pytest.MonkeyPatch, *responses: _Response) -> _HttpSession:
    session = _HttpSession(list(responses))
    monkeypatch.setattr(google_mcp.aiohttp, "ClientSession", session)
    return session


class TestConfig:
    def test_defaults_and_derived_values(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)

        assert config.key == "test_gmcp"
        assert config.mcp_url() == "https://testmcp.example/mcp"
        assert config.redirect_uri() == "http://localhost:8765/oauth/callback"
        assert config.scopes() == "scope.read"
        assert config.headers() == {}
        assert config.config_status() == {
            "test_gmcp_url": "https://testmcp.example/mcp",
            "test_gmcp_auth": False,
            "test_gmcp_oauth_client": False,
            "test_gmcp_redirect_uri": None,
            "test_gmcp_token_file": None,
        }

    def test_a_static_token_becomes_a_bearer_header(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _config(tmp_path, monkeypatch, TOKEN="abc").headers() == {
            "Authorization": "Bearer abc"
        }

    def test_an_explicit_authorization_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch, TOKEN="abc", AUTHORIZATION="Basic xyz")

        assert config.headers() == {"Authorization": "Basic xyz"}

    def test_an_oauth_client_reports_where_its_token_lives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status = _config(tmp_path, monkeypatch, CLIENT_ID="id").config_status()

        assert status["test_gmcp_auth"] is True
        assert status["test_gmcp_token_file"] == str(tmp_path / "tokens.json")

    def test_the_default_token_file_is_under_the_home_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        monkeypatch.delenv(f"{PREFIX}_TOKEN_FILE")

        assert config.token_file() == str(Path.home() / ".wactorz" / "test.json")

    @pytest.mark.parametrize(("value", "expected"), [("5", 5.0), ("soon", 20.0)])
    def test_the_mcp_timeout_is_configurable(
        self, monkeypatch: pytest.MonkeyPatch, value: str, expected: float
    ) -> None:
        monkeypatch.setenv("GOOGLE_MCP_TIMEOUT", value)

        assert mcp_timeout() == expected


class TestFormatting:
    def test_structured_content_is_rendered_as_json(self) -> None:
        class _Result:
            def __init__(self) -> None:
                self.structuredContent = {"events": []}  # the MCP field name

        assert json.loads(format_mcp_content(_Result())) == {"events": []}

    def test_content_parts_are_joined(self) -> None:
        class _Text:
            text = "hello"

        class _Dumpable:
            def model_dump_json(self, indent: int = 0) -> str:
                return '{"k": 1}'

        class _Opaque:
            def model_dump_json(self, indent: int = 0) -> str:
                raise ValueError("no")

            def __str__(self) -> str:
                return "opaque"

        class _Result:
            def __init__(self) -> None:
                self.content = [_Text(), _Dumpable(), _Opaque()]

        assert format_mcp_content(_Result()) == 'hello\n{"k": 1}\nopaque'

    def test_a_result_with_no_content_is_its_string(self) -> None:
        assert format_mcp_content("plain") == "plain"

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"error": {"message": "Not Found"}}, "Not Found"),
            ({"error": "invalid_grant"}, "invalid_grant"),
            ({"error": {"code": 500}}, "{'error': {'code': 500}}"),
            ("boom", "boom"),
        ],
    )
    def test_rest_error_messages(self, data: Any, message: str) -> None:
        assert rest_error_message(data) == message


class TestCallTool:
    @staticmethod
    def _client(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mcp_result: str, rest: Any
    ) -> tuple[GoogleMcpClient, list[str]]:
        client = GoogleMcpClient(_config(tmp_path, monkeypatch))
        rest_calls: list[str] = []

        async def _mcp(tool: str, args: dict[str, Any], interactive: bool = False) -> str:
            if mcp_result == "hang":
                await asyncio.sleep(60)
            return mcp_result

        async def _rest(tool: str, args: dict[str, Any]) -> Any:
            rest_calls.append(tool)
            return rest

        monkeypatch.setattr(client, "_call_mcp", _mcp)
        monkeypatch.setattr(client, "_call_rest", _rest)
        return client, rest_calls

    async def test_a_successful_mcp_result_is_returned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, rest_calls = self._client(tmp_path, monkeypatch, "events: 3", "rest")

        assert await client.call_tool("list_events") == "events: 3"
        assert rest_calls == []

    @pytest.mark.parametrize(
        "denied",
        [
            "Caller does not have permission",
            "PERMISSION_DENIED",
            "Test MCP error: connection reset",
            "The 'mcp' package is required.",
        ],
    )
    async def test_a_refused_mcp_call_is_served_by_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: str
    ) -> None:
        client, rest_calls = self._client(tmp_path, monkeypatch, denied, "from rest")

        assert await client.call_tool("list_events", {"q": "x"}) == "from rest"
        assert rest_calls == ["list_events"]

    async def test_without_a_rest_handler_the_mcp_error_stands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = self._client(tmp_path, monkeypatch, "PERMISSION_DENIED", None)

        assert await client.call_tool("list_events") == "PERMISSION_DENIED"

    async def test_a_hung_mcp_call_times_out_to_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_MCP_TIMEOUT", "0.01")
        client, rest_calls = self._client(tmp_path, monkeypatch, "hang", "from rest")

        assert await client.call_tool("list_events") == "from rest"
        assert rest_calls == ["list_events"]


class _Tool:
    def __init__(self, name: str, description: str | None) -> None:
        self.name = name
        self.description = description


class _Session:
    def __init__(self, read: Any, write: Any) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def initialize(self) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "explode":
            raise RuntimeError("server closed")
        return f"{name}:{arguments}"

    async def list_tools(self) -> Any:
        class _Tools:
            def __init__(self) -> None:
                self.tools = [_Tool("list_events", "Lists events"), _Tool("whoami", None)]

        return _Tools()


class _Transport:
    def __init__(self) -> None:
        self.opened: list[tuple[str, Any, Any]] = []

    def __call__(self, url: str, headers: Any = None, auth: Any = None) -> "_Transport":
        self.opened.append((url, headers, auth))
        return self

    async def __aenter__(self) -> tuple[str, str, None]:
        return "read", "write", None

    async def __aexit__(self, *_exc: object) -> None:
        return None


class TestMcpTransport:
    @pytest.fixture(name="transport")
    def transport_fixture(self, monkeypatch: pytest.MonkeyPatch) -> _Transport:
        transport = _Transport()
        monkeypatch.setattr(google_mcp, "streamablehttp_client", transport)
        monkeypatch.setattr(google_mcp, "ClientSession", _Session)
        return transport

    async def test_without_the_mcp_extra_it_says_how_to_install_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(google_mcp, "ClientSession", None)
        client = GoogleMcpClient(_config(tmp_path, monkeypatch))

        assert "pip install wactorz[mcp]" in await client._call_mcp("x", {})
        assert "pip install wactorz[mcp]" in await client.list_tools()

    async def test_a_tool_is_called_with_the_static_headers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: _Transport
    ) -> None:
        client = GoogleMcpClient(_config(tmp_path, monkeypatch, TOKEN="abc"))

        result = await client._call_mcp("list_events", {"max": 1})

        assert result == "list_events:{'max': 1}"
        assert transport.opened == [
            ("https://testmcp.example/mcp", {"Authorization": "Bearer abc"}, None)
        ]

    async def test_a_failing_session_is_reported_as_an_mcp_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: _Transport
    ) -> None:
        client = GoogleMcpClient(_config(tmp_path, monkeypatch))

        assert await client._call_mcp("explode", {}) == "Test MCP error: server closed"

    async def test_tools_are_listed_one_per_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: _Transport
    ) -> None:
        client = GoogleMcpClient(_config(tmp_path, monkeypatch))

        assert await client.list_tools() == "list_events: Lists events\nwhoami:"

    async def test_a_failing_listing_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: _Transport
    ) -> None:
        def _broken(config: GoogleMcpConfig, interactive: bool = False) -> Any:
            raise RuntimeError("bad client")

        monkeypatch.setattr(google_mcp, "build_auth", _broken)
        client = GoogleMcpClient(_config(tmp_path, monkeypatch))

        assert await client.list_tools() == "Test MCP error: bad client"


class TestLogin:
    async def test_a_stored_token_is_authorized_even_if_mcp_is_gated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = GoogleMcpClient(_config(tmp_path, monkeypatch, TOKEN="abc"))
        results = iter(["PERMISSION_DENIED", "ok"])

        async def _mcp(tool: str, args: dict[str, Any], interactive: bool = False) -> str:
            assert (tool, interactive) == ("whoami", True)
            return next(results)

        monkeypatch.setattr(client, "_call_mcp", _mcp)

        assert "REST fallback will be used" in await client.login()
        assert await client.login() == "Test: authorized."

    async def test_no_token_means_login_did_not_complete(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = GoogleMcpClient(_config(tmp_path, monkeypatch))

        async def _mcp(tool: str, args: dict[str, Any], interactive: bool = False) -> str:
            return "cancelled"

        monkeypatch.setattr(client, "_call_mcp", _mcp)

        assert await client.login() == "Test login did not complete: cancelled"


class TestRestFallback:
    async def test_an_unknown_tool_has_no_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert await GoogleMcpClient(_config(tmp_path, monkeypatch))._call_rest("x", {}) is None

    async def test_a_handler_error_is_shown_and_a_transport_error_is_not(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Client(GoogleMcpClient):
            def _rest_handlers(self) -> dict[str, Any]:
                async def _google(args: dict[str, Any]) -> str:
                    raise GoogleRestError("Calendar not found")

                async def _transport(args: dict[str, Any]) -> str:
                    raise OSError("connection reset")

                async def _ok(args: dict[str, Any]) -> str:
                    return "fine"

                return {"google": _google, "transport": _transport, "ok": _ok}

        client = _Client(_config(tmp_path, monkeypatch))

        assert await client._call_rest("google", {}) == "Test error: Calendar not found"
        assert await client._call_rest("transport", {}) is None
        assert await client._call_rest("ok", {}) == "fine"


class TestTokens:
    def test_a_static_token_wins_over_the_stored_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        _write_tokens(config, {"access_token": "stored"})
        client = GoogleMcpClient(config)

        assert client._access_token() == "stored"
        monkeypatch.setenv(f"{PREFIX}_TOKEN", "static")
        assert client._access_token() == "static"

    def test_an_unreadable_token_file_is_no_tokens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        Path(config.token_file()).write_text("not json", encoding="utf-8")

        assert GoogleMcpClient(config)._stored_tokens() == {}

    def test_a_token_response_is_merged_into_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        _write_tokens(config, {"refresh_token": "keep"}, client_info={"id": 1})
        client = GoogleMcpClient(config)

        client._store_token_response({"access_token": "new", "scope": "s"})

        assert _stored(config) == {
            "tokens": {
                "refresh_token": "keep",
                "access_token": "new",
                "scope": "s",
                "token_type": "Bearer",
            },
            "client_info": {"id": 1},
        }

    def test_a_token_response_with_no_file_yet_creates_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)

        GoogleMcpClient(config)._store_token_response({"refresh_token": "r", "token_type": "x"})

        assert _stored(config)["tokens"] == {"refresh_token": "r", "token_type": "x"}

    def test_a_token_that_cannot_be_written_is_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        monkeypatch.setenv(f"{PREFIX}_TOKEN_FILE", str(tmp_path / "file" / "tokens.json"))
        (tmp_path / "file").write_text("a file where a directory should be", encoding="utf-8")
        client = GoogleMcpClient(config)

        client._store_token_response({"access_token": "a"})
        client._save_access_token("a")

    def test_a_refreshed_access_token_is_saved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        client = GoogleMcpClient(config)

        client._save_access_token("fresh")

        assert _stored(config) == {"tokens": {"access_token": "fresh"}}

    async def test_refreshing_needs_a_refresh_token_and_a_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        _write_tokens(config, {"refresh_token": "r"})

        assert await GoogleMcpClient(config)._refresh_access_token() is None

    async def test_a_refresh_saves_the_new_access_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch, CLIENT_ID="id", CLIENT_SECRET="secret")
        _write_tokens(config, {"refresh_token": "r"})
        session = _http(monkeypatch, _Response(200, {"access_token": "fresh"}))

        assert await GoogleMcpClient(config)._refresh_access_token() == "fresh"
        assert _stored(config)["tokens"]["access_token"] == "fresh"
        ((_, url, kwargs),) = session.requests
        assert url == google_mcp.GOOGLE_TOKEN_URL
        assert kwargs["data"]["grant_type"] == "refresh_token"

    async def test_a_refused_refresh_is_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch, CLIENT_ID="id", CLIENT_SECRET="secret")
        _write_tokens(config, {"refresh_token": "r"})
        _http(monkeypatch, _Response(400, {"error": "invalid_grant"}))

        assert await GoogleMcpClient(config)._refresh_access_token() is None


class TestRestRequest:
    async def test_without_a_token_the_request_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(GoogleRestError, match="Not authenticated"):
            await GoogleMcpClient(_config(tmp_path, monkeypatch))._rest_request("GET", "/x")

    async def test_a_relative_path_is_joined_to_the_api_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = _http(monkeypatch, _Response(200, {"items": []}))
        client = GoogleMcpClient(_config(tmp_path, monkeypatch, TOKEN="abc"))

        result = await client._rest_request("GET", "/events", params={"q": "x"})

        assert result == (200, {"items": []})
        ((method, url, kwargs),) = session.requests
        assert (method, url) == ("GET", "https://api.example/v1/events")
        assert kwargs["headers"] == {"Authorization": "Bearer abc"}

    @pytest.mark.parametrize(
        ("response", "body"),
        [
            (_Response(204), {}),
            (_Response(200, ["a", "list"]), {"raw": ["a", "list"]}),
            (_Response(502, ValueError("not json"), text="Bad Gateway"), {"raw": "Bad Gateway"}),
        ],
    )
    async def test_every_body_is_handed_back_as_a_dict(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: _Response, body: Any
    ) -> None:
        _http(monkeypatch, response)
        client = GoogleMcpClient(_config(tmp_path, monkeypatch, TOKEN="abc"))

        assert (await client._rest_request("DELETE", "https://other.example/x"))[1] == body

    async def test_a_401_is_retried_once_with_a_refreshed_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch, CLIENT_ID="id", CLIENT_SECRET="secret")
        _write_tokens(config, {"access_token": "old", "refresh_token": "r"})
        session = _http(
            monkeypatch,
            _Response(401, {"error": "expired"}),
            _Response(200, {"access_token": "new"}),
            _Response(200, {"ok": True}),
        )

        result = await GoogleMcpClient(config)._rest_request("GET", "/x")

        assert result == (200, {"ok": True})
        assert session.requests[-1][2]["headers"] == {"Authorization": "Bearer new"}


class TestAuthorizeDirect:
    @staticmethod
    def _flow(monkeypatch: pytest.MonkeyPatch, code: str, state: str | None) -> list[str]:
        opened: list[str] = []

        def _callback(config: GoogleMcpConfig) -> Any:
            async def _wait() -> tuple[str, str | None]:
                if state == "echo":
                    while not opened:
                        await asyncio.sleep(0)
                    sent = dict(p.split("=", 1) for p in opened[0].split("?", 1)[1].split("&"))
                    return code, sent["state"]
                return code, state

            return _wait

        def _redirect(config: GoogleMcpConfig) -> Any:
            async def _open(url: str) -> None:
                opened.append(url)

            return _open

        real_sleep = asyncio.sleep

        async def _instant(_delay: float) -> None:
            await real_sleep(0)

        monkeypatch.setattr(google_mcp, "_make_callback_handler", _callback)
        monkeypatch.setattr(google_mcp, "_make_redirect_handler", _redirect)
        monkeypatch.setattr(google_mcp.asyncio, "sleep", _instant)
        return opened

    async def test_without_a_client_nothing_starts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = GoogleMcpClient(_config(tmp_path, monkeypatch))

        assert await client.authorize_direct() == "Test: no OAuth client configured"

    async def test_a_cancelled_consent_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._flow(monkeypatch, code="", state=None)
        client = GoogleMcpClient(_config(tmp_path, monkeypatch, CLIENT_ID="i", CLIENT_SECRET="s"))

        assert await client.authorize_direct() == "Test: authorization was cancelled"

    async def test_a_callback_with_the_wrong_state_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._flow(monkeypatch, code="code", state="forged")
        client = GoogleMcpClient(_config(tmp_path, monkeypatch, CLIENT_ID="i", CLIENT_SECRET="s"))

        assert "failed a security check" in await client.authorize_direct()

    async def test_a_matching_callback_exchanges_the_code_and_stores_the_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        opened = self._flow(monkeypatch, code="code", state="echo")
        session = _http(monkeypatch, _Response(200, {"access_token": "a", "refresh_token": "r"}))
        config = _config(tmp_path, monkeypatch, CLIENT_ID="i", CLIENT_SECRET="s")

        result = await GoogleMcpClient(config).authorize_direct(scopes="only.this")

        assert result == "Test: authorized (direct REST)."
        assert "scope=only.this" in opened[0]
        assert "access_type=offline" in opened[0]
        assert session.requests[0][2]["data"]["code"] == "code"
        assert _stored(config)["tokens"]["refresh_token"] == "r"

    async def test_a_failed_exchange_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._flow(monkeypatch, code="code", state="echo")
        _http(monkeypatch, _Response(400, {"error": {"message": "bad code"}}))
        client = GoogleMcpClient(_config(tmp_path, monkeypatch, CLIENT_ID="i", CLIENT_SECRET="s"))

        assert await client.authorize_direct() == "Test token exchange failed: bad code"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TestCallbackServer:
    async def test_the_code_and_state_arrive_on_the_configured_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        port = _free_port()
        config = _config(
            tmp_path, monkeypatch, REDIRECT_URI=f"http://127.0.0.1:{port}/oauth/callback"
        )
        waiting = asyncio.create_task(google_mcp._make_callback_handler(config)())
        base = f"http://127.0.0.1:{port}"

        async with aiohttp.ClientSession() as http:
            for _ in range(200):
                try:
                    async with http.get(f"{base}/elsewhere") as wrong:
                        assert wrong.status == 404
                    break
                except aiohttp.ClientConnectionError:
                    await asyncio.sleep(0.01)
            async with http.get(f"{base}/oauth/callback?code=c1&state=s1") as right:
                assert "authentication complete" in await right.text()

        # `asyncio.wait`, not `wait_for`, which can lose a cancellation on 3.10/3.11.
        done, _ = await asyncio.wait({waiting}, timeout=5)
        assert waiting in done
        assert waiting.result() == ("c1", "s1")

    async def test_the_browser_handler_prints_the_url_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _no_browser(url: str) -> bool:
            raise RuntimeError("no display")

        monkeypatch.setattr(google_mcp.webbrowser, "open", _no_browser)

        await google_mcp._make_redirect_handler(_config(tmp_path, monkeypatch))("https://consent")

        assert "https://consent" in capsys.readouterr().err

    async def test_a_non_interactive_flow_refuses_to_open_a_browser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = google_mcp._make_noninteractive_handler(_config(tmp_path, monkeypatch))

        with pytest.raises(RuntimeError, match="run login\\(\\) once first"):
            await handler("https://consent")


@needs_mcp
class TestTokenStorage:
    async def test_tokens_round_trip_and_keep_the_refresh_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        storage = _GoogleTokenStorage(config)
        assert google_mcp.OAuthToken is not None

        assert await storage.get_tokens() is None
        await storage.set_tokens(
            google_mcp.OAuthToken(access_token="a1", token_type="Bearer", refresh_token="r1")
        )
        await storage.set_tokens(google_mcp.OAuthToken(access_token="a2", token_type="Bearer"))

        tokens = await storage.get_tokens()
        assert tokens is not None
        assert (tokens.access_token, tokens.refresh_token) == ("a2", "r1")

    async def test_client_info_comes_from_the_environment_or_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        storage = _GoogleTokenStorage(config)
        assert google_mcp.OAuthClientInformationFull is not None

        assert await storage.get_client_info() is None
        await storage.set_client_info(
            google_mcp.OAuthClientInformationFull(
                client_id="stored",
                redirect_uris=["http://localhost/cb"],  # pyright: ignore[reportArgumentType]
            )
        )
        stored = await storage.get_client_info()
        assert stored is not None and stored.client_id == "stored"

        monkeypatch.setenv(f"{PREFIX}_CLIENT_ID", "env")
        monkeypatch.setenv(f"{PREFIX}_CLIENT_SECRET", "secret")
        from_env = await storage.get_client_info()
        assert from_env is not None and from_env.client_id == "env"

    def test_an_unreadable_file_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _config(tmp_path, monkeypatch)
        Path(config.token_file()).write_text("{broken", encoding="utf-8")

        assert _GoogleTokenStorage(config)._read() == {}

    def test_auth_is_only_built_with_a_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert build_auth(_config(tmp_path, monkeypatch)) is None

        config = _config(tmp_path, monkeypatch, CLIENT_ID="i", CLIENT_SECRET="s")

        assert build_auth(config) is not None
        assert build_auth(config, interactive=True) is not None
