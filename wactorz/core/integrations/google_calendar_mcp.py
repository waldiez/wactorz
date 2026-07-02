"""Google Calendar MCP integration client."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import web

try:
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
except ImportError:  # pragma: no cover - optional dependency guard
    ClientSession = None
    OAuthClientProvider = None
    TokenStorage = object
    streamablehttp_client = None
    OAuthClientInformationFull = None
    OAuthClientMetadata = None
    OAuthToken = None

logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_MCP_URL = "https://calendarmcp.googleapis.com/mcp/v1"
DEFAULT_CALENDAR_MCP_SCOPES = (
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly "
    "https://www.googleapis.com/auth/calendar.events "
    "https://www.googleapis.com/auth/calendar.events.freebusy "
    "https://www.googleapis.com/auth/calendar.events.readonly"
)


def calendar_mcp_config_status() -> dict[str, Any]:
    client_id = os.getenv("CALENDAR_MCP_CLIENT_ID", "")
    return {
        "calendar_mcp_url": calendar_mcp_url() or None,
        "calendar_mcp_auth": bool(
            client_id
            or os.getenv("CALENDAR_MCP_TOKEN", "")
            or os.getenv("CALENDAR_MCP_AUTHORIZATION", "")
        ),
        "calendar_mcp_oauth_client": bool(client_id),
        "calendar_mcp_redirect_uri": calendar_mcp_redirect_uri() if client_id else None,
        "calendar_mcp_token_file": calendar_mcp_token_file() if client_id else None,
    }


def calendar_mcp_url() -> str:
    return os.getenv("CALENDAR_MCP_URL", GOOGLE_CALENDAR_MCP_URL).rstrip("/")


def calendar_mcp_redirect_uri() -> str:
    return os.getenv("CALENDAR_MCP_REDIRECT_URI", "http://localhost:8765/oauth/callback")


def calendar_mcp_scopes() -> str:
    return os.getenv("CALENDAR_MCP_SCOPES", DEFAULT_CALENDAR_MCP_SCOPES)


def calendar_mcp_token_file() -> str:
    return os.getenv("CALENDAR_MCP_TOKEN_FILE") or str(
        Path.home() / ".wactorz" / "calendar_mcp_token.json"
    )


def _headers() -> dict[str, str]:
    auth = os.getenv("CALENDAR_MCP_AUTHORIZATION", "")
    if not auth and os.getenv("CALENDAR_MCP_TOKEN", ""):
        auth = f"Bearer {os.getenv('CALENDAR_MCP_TOKEN')}"
    return {"Authorization": auth} if auth else {}


class _CalendarMcpTokenStorage(TokenStorage):
    def __init__(self, token_file: str):
        self.path = Path(token_file).expanduser()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            logger.warning("Could not read Calendar MCP token file %s", self.path)
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception:
            pass

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        client_id = os.getenv("CALENDAR_MCP_CLIENT_ID", "")
        client_secret = os.getenv("CALENDAR_MCP_CLIENT_SECRET", "")
        if client_id and client_secret:
            return OAuthClientInformationFull(
                client_id=client_id,
                client_secret=client_secret,
                token_endpoint_auth_method="client_secret_post",
                redirect_uris=[calendar_mcp_redirect_uri()],
            )
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


async def _open_oauth_browser(url: str) -> None:
    print(
        f"\nCalendar MCP OAuth required. Open this URL if the browser does not open:\n{url}\n",
        file=sys.stderr,
    )
    try:
        webbrowser.open(url)
    except Exception:
        pass


async def _wait_for_oauth_callback() -> tuple[str, str | None]:
    parsed = urlparse(calendar_mcp_redirect_uri())
    host = parsed.hostname or "localhost"
    port = parsed.port or 8765
    path = parsed.path or "/oauth/callback"
    loop = asyncio.get_running_loop()
    future: asyncio.Future[tuple[str, str | None]] = loop.create_future()

    async def callback(request: web.Request) -> web.Response:
        if request.path != path:
            return web.Response(status=404, text="Not found")
        code = request.query.get("code", "")
        state = request.query.get("state")
        if not future.done():
            future.set_result((code, state))
        return web.Response(text="Calendar MCP authentication complete. You can close this window.")

    app = web.Application()
    app.router.add_get(path, callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        return await future
    finally:
        await runner.cleanup()


def _auth():
    client_id = os.getenv("CALENDAR_MCP_CLIENT_ID", "")
    client_secret = os.getenv("CALENDAR_MCP_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        return None
    metadata = OAuthClientMetadata(
        redirect_uris=[calendar_mcp_redirect_uri()],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=calendar_mcp_scopes(),
        client_name="Wactorz Calendar MCP",
    )
    return OAuthClientProvider(
        server_url=calendar_mcp_url(),
        client_metadata=metadata,
        storage=_CalendarMcpTokenStorage(calendar_mcp_token_file()),
        redirect_handler=_open_oauth_browser,
        callback_handler=_wait_for_oauth_callback,
    )


def format_mcp_content(result: Any) -> str:
    if getattr(result, "structuredContent", None) is not None:
        return json.dumps(result.structuredContent, indent=2)
    content = getattr(result, "content", None)
    if not content:
        return str(result)
    parts = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
        else:
            try:
                parts.append(item.model_dump_json(indent=2))
            except Exception:
                parts.append(str(item))
    return "\n".join(parts)


class GoogleCalendarMcpClient:
    """Client for Google's hosted Calendar MCP server."""

    async def call_tool(self, tool_name: str, arguments: dict | None = None) -> str:
        if ClientSession is None or streamablehttp_client is None:
            return "The 'mcp' package is required. Install with: pip install wactorz[mcp]"
        try:
            auth = _auth()
            async with streamablehttp_client(
                calendar_mcp_url(),
                headers={} if auth else _headers(),
                auth=auth,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments or {})
                    return format_mcp_content(result)
        except Exception as exc:
            return f"Calendar MCP error: {exc}"

    async def list_tools(self) -> str:
        if ClientSession is None or streamablehttp_client is None:
            return "The 'mcp' package is required. Install with: pip install wactorz[mcp]"
        try:
            auth = _auth()
            async with streamablehttp_client(
                calendar_mcp_url(),
                headers={} if auth else _headers(),
                auth=auth,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return "\n".join(
                        f"{tool.name}: {tool.description or ''}".rstrip()
                        for tool in tools.tools
                    )
        except Exception as exc:
            return f"Calendar MCP error: {exc}"
