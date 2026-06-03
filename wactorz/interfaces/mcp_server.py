"""
MCP Server - Exposes wactorz capabilities as Model Context Protocol tools.

Any MCP-compatible client (Claude Desktop, Cursor, Zed, etc.) can connect
and drive a running wactorz instance: chat with the orchestrator, manage
live agents, query Fuseki, and control Home Assistant entities directly.

Run:      wactorz-mcp                           (stdio transport, for Claude Desktop)
Env:      WACTORZ_URL       (default http://localhost:8000)
          WACTORZ_API_KEY   (optional, matches wactorz REST API_KEY)
          HA_URL / HA_TOKEN (optional, enables direct HA tools)

Claude Desktop config (~/.claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "wactorz": {
        "command": "wactorz-mcp",
        "env": {
          "WACTORZ_URL": "http://localhost:8000",
          "HA_URL": "http://homeassistant.local:8123",
          "HA_TOKEN": "..."
        }
      }
    }
  }
"""

import json
import logging
import os
import stat
import asyncio
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp
from aiohttp import web

try:
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.client.streamable_http import streamablehttp_client
    from mcp.server.fastmcp import FastMCP
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
except ImportError as exc:
    raise SystemExit(
        "The 'mcp' package is required. Install with: pip install wactorz[mcp]"
    ) from exc

logger = logging.getLogger(__name__)

WACTORZ_URL = os.getenv("WACTORZ_URL", "http://localhost:8000").rstrip("/")
WACTORZ_API_KEY = os.getenv("WACTORZ_API_KEY", "")
HA_URL = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
CALENDAR_MCP_URL = os.getenv("CALENDAR_MCP_URL", "").rstrip("/")
CALENDAR_MCP_TOKEN = os.getenv("CALENDAR_MCP_TOKEN", "")
CALENDAR_MCP_AUTHORIZATION = os.getenv("CALENDAR_MCP_AUTHORIZATION", "")
CALENDAR_MCP_CLIENT_ID = os.getenv("CALENDAR_MCP_CLIENT_ID", "")
CALENDAR_MCP_CLIENT_SECRET = os.getenv("CALENDAR_MCP_CLIENT_SECRET", "")
CALENDAR_MCP_REDIRECT_URI = os.getenv(
    "CALENDAR_MCP_REDIRECT_URI",
    "http://localhost:8765/oauth/callback",
)
CALENDAR_MCP_SCOPES = os.getenv(
    "CALENDAR_MCP_SCOPES",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly "
    "https://www.googleapis.com/auth/calendar.events.freebusy "
    "https://www.googleapis.com/auth/calendar.events.readonly",
)
CALENDAR_MCP_TOKEN_FILE = (
    os.getenv("CALENDAR_MCP_TOKEN_FILE")
    or str(Path.home() / ".wactorz" / "calendar_mcp_token.json")
)

mcp = FastMCP("wactorz")


def _wactorz_headers() -> dict[str, str]:
    return {"X-API-Key": WACTORZ_API_KEY} if WACTORZ_API_KEY else {}


def _ha_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json",
    }


async def _wactorz_post(path: str, payload: dict) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{WACTORZ_URL}{path}", json=payload, headers=_wactorz_headers()
            ) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"status": resp.status, "text": text}
    except aiohttp.ClientConnectorError:
        return {"error": f"Cannot connect to wactorz at {WACTORZ_URL}. Is it running?"}
    except Exception as exc:
        return {"error": str(exc)}


async def _wactorz_get(path: str) -> Any:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{WACTORZ_URL}{path}", headers=_wactorz_headers()) as resp:
                text = await resp.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
    except aiohttp.ClientConnectorError:
        return {"error": f"Cannot connect to wactorz at {WACTORZ_URL}. Is it running?"}
    except Exception as exc:
        return {"error": str(exc)}


async def _wactorz_delete(path: str) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{WACTORZ_URL}{path}", headers=_wactorz_headers()
            ) as resp:
                return {"status": resp.status, "text": await resp.text()}
    except aiohttp.ClientConnectorError:
        return {"status": 0, "text": f"Cannot connect to wactorz at {WACTORZ_URL}. Is it running?"}
    except Exception as exc:
        return {"status": 0, "text": str(exc)}


# ─── Orchestrator tools ─────────────────────────────────────────────────────


@mcp.tool()
async def ask_wactorz(message: str) -> str:
    """
    Send a message to the wactorz main orchestrator.

    The orchestrator will route to an existing agent, spawn a new one, or
    answer directly. Use for fuzzy requests ("monitor my CPU temp",
    "what's the weather") where you don't know which agent should handle it.
    """
    data = await _wactorz_post("/chat", {"message": message})
    return str(data.get("response", data))


@mcp.tool()
async def ask_agent(agent_name: str, message: str) -> str:
    """
    Send a message directly to a named agent, bypassing the orchestrator.

    Use when you already know which agent should handle the request.
    Agent names are lowercase-hyphenated (e.g. "home-assistant-agent").
    Run list_agents() first if unsure.
    """
    data = await _wactorz_post("/chat", {"message": message, "agent_name": agent_name})
    return str(data.get("response", data))


# ─── Remote Calendar MCP tools ──────────────────────────────────────────────


def _calendar_mcp_headers() -> dict[str, str]:
    auth = CALENDAR_MCP_AUTHORIZATION
    if not auth and CALENDAR_MCP_TOKEN:
        auth = f"Bearer {CALENDAR_MCP_TOKEN}"
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
        if CALENDAR_MCP_CLIENT_ID and CALENDAR_MCP_CLIENT_SECRET:
            return OAuthClientInformationFull(
                client_id=CALENDAR_MCP_CLIENT_ID,
                client_secret=CALENDAR_MCP_CLIENT_SECRET,
                token_endpoint_auth_method="client_secret_post",
                redirect_uris=[CALENDAR_MCP_REDIRECT_URI],
            )
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)


async def _open_oauth_browser(url: str) -> None:
    print(f"\nCalendar MCP OAuth required. Open this URL if the browser does not open:\n{url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass


async def _wait_for_oauth_callback() -> tuple[str, str | None]:
    parsed = urlparse(CALENDAR_MCP_REDIRECT_URI)
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


def _calendar_mcp_auth():
    if not (CALENDAR_MCP_CLIENT_ID and CALENDAR_MCP_CLIENT_SECRET):
        return None
    metadata = OAuthClientMetadata(
        redirect_uris=[CALENDAR_MCP_REDIRECT_URI],
        token_endpoint_auth_method="client_secret_post",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=CALENDAR_MCP_SCOPES,
        client_name="Wactorz Calendar MCP",
    )
    return OAuthClientProvider(
        server_url=CALENDAR_MCP_URL,
        client_metadata=metadata,
        storage=_CalendarMcpTokenStorage(CALENDAR_MCP_TOKEN_FILE),
        redirect_handler=_open_oauth_browser,
        callback_handler=_wait_for_oauth_callback,
    )


def _format_mcp_content(result: Any) -> str:
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


async def _calendar_mcp_call(tool_name: str, arguments: dict | None = None) -> str:
    """Call a tool on a configured remote Calendar MCP server."""
    if not CALENDAR_MCP_URL:
        return (
            "Calendar MCP is not configured. Set CALENDAR_MCP_URL and, if needed, "
            "CALENDAR_MCP_TOKEN or CALENDAR_MCP_AUTHORIZATION."
        )
    try:
        auth = _calendar_mcp_auth()
        async with streamablehttp_client(
            CALENDAR_MCP_URL,
            headers={} if auth else _calendar_mcp_headers(),
            auth=auth,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments or {})
                return _format_mcp_content(result)
    except Exception as exc:
        return f"Calendar MCP error: {exc}"


@mcp.tool()
async def calendar_status() -> str:
    """
    Show remote Calendar MCP configuration status.

    This does not expose tokens. Use calendar_mcp_list_tools to verify the
    remote server is reachable.
    """
    return json.dumps(
        {
            "calendar_mcp_url": CALENDAR_MCP_URL or None,
            "calendar_mcp_auth": bool(
                CALENDAR_MCP_CLIENT_ID
                or CALENDAR_MCP_TOKEN
                or CALENDAR_MCP_AUTHORIZATION
            ),
            "calendar_mcp_oauth_client": bool(CALENDAR_MCP_CLIENT_ID),
            "calendar_mcp_redirect_uri": CALENDAR_MCP_REDIRECT_URI if CALENDAR_MCP_CLIENT_ID else None,
            "calendar_mcp_token_file": CALENDAR_MCP_TOKEN_FILE if CALENDAR_MCP_CLIENT_ID else None,
        },
        indent=2,
    )


@mcp.tool()
async def calendar_mcp_list_tools() -> str:
    """List tools exposed by the configured remote Calendar MCP server."""
    if not CALENDAR_MCP_URL:
        return (
            "Calendar MCP is not configured. Set CALENDAR_MCP_URL and, if needed, "
            "CALENDAR_MCP_TOKEN or CALENDAR_MCP_AUTHORIZATION."
        )
    try:
        auth = _calendar_mcp_auth()
        async with streamablehttp_client(
            CALENDAR_MCP_URL,
            headers={} if auth else _calendar_mcp_headers(),
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


@mcp.tool()
async def calendar_mcp_call_tool(tool_name: str, arguments_json: str = "{}") -> str:
    """
    Call any tool exposed by the configured remote Calendar MCP server.

    arguments_json must be a JSON object string. Use calendar_mcp_list_tools()
    to discover the exact tool names and schemas.
    """
    try:
        arguments = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as exc:
        return f"Invalid arguments_json: {exc}"
    if not isinstance(arguments, dict):
        return "arguments_json must encode a JSON object."
    return await _calendar_mcp_call(tool_name, arguments)


@mcp.tool()
async def calendar_list(count: int = 10) -> str:
    """
    List upcoming events through the configured remote Calendar MCP server.

    Assumes the remote server exposes a list_events tool.
    """
    return await _calendar_mcp_call("list_events", {"maxResults": count})


@mcp.tool()
async def calendar_today() -> str:
    """List today's events through the configured remote Calendar MCP server."""
    return await _calendar_mcp_call("list_events", {"range": "today"})


@mcp.tool()
async def calendar_week() -> str:
    """List this week's events through the configured remote Calendar MCP server."""
    return await _calendar_mcp_call("list_events", {"range": "week"})


@mcp.tool()
async def calendar_create_event(
    summary: str,
    start: str,
    end: str = "",
    location: str = "",
    description: str = "",
) -> str:
    """
    Create an event through the configured remote Calendar MCP server.

    start/end should be ISO-8601 datetimes, e.g. 2026-06-03T15:00:00+03:00.
    """
    payload = {"summary": summary, "startDateTime": start}
    if end:
        payload["endDateTime"] = end
    if location:
        payload["location"] = location
    if description:
        payload["description"] = description
    return await _calendar_mcp_call("create_event", payload)


@mcp.tool()
async def calendar_delete_event(event_id: str) -> str:
    """Delete a calendar event through the configured remote Calendar MCP server."""
    return await _calendar_mcp_call("delete_event", {"eventId": event_id})


# ─── Agent management tools ─────────────────────────────────────────────────


@mcp.tool()
async def list_agents() -> str:
    """List all currently running agents with their id, name, and state."""
    agents = await _wactorz_get("/agents")
    if isinstance(agents, dict) and agents.get("error"):
        return str(agents["error"])
    if not isinstance(agents, list) or not agents:
        return "No agents running."
    lines = []
    for a in agents:
        protected = " [protected]" if a.get("protected") else ""
        state = a.get("state", "unknown")
        name = a.get("name", "?")
        aid = a.get("id", "?")  # full ID required for stop/pause/resume
        lines.append(f"[{state:10s}] @{name}{protected}  (id: {aid})")
    return "\n".join(lines)


@mcp.tool()
async def list_capabilities(keyword: str = "") -> str:
    """
    List the full agent capability catalog (running + spawnable).

    Each entry shows name, description, capabilities, and whether the agent
    is running or available as a catalog recipe. Filter with an optional
    keyword (e.g. "weather", "pdf", "sensor").
    """
    cmd = f"/capabilities {keyword}".strip()
    data = await _wactorz_post("/chat", {"message": cmd})
    return str(data.get("response", data))


@mcp.tool()
async def stop_agent(agent_id: str) -> str:
    """Stop a running agent by its id (first 8 chars from list_agents is enough)."""
    result = await _wactorz_delete(f"/actors/{agent_id}")
    if result.get("status") == 200:
        return f"Agent {agent_id} stopped."
    return f"Error {result.get('status')}: {result.get('text')}"


@mcp.tool()
async def pause_agent(agent_id: str) -> str:
    """Pause a running agent. It stops processing messages until resumed."""
    data = await _wactorz_post(f"/actors/{agent_id}/pause", {})
    return str(data)


@mcp.tool()
async def resume_agent(agent_id: str) -> str:
    """Resume a paused agent."""
    data = await _wactorz_post(f"/actors/{agent_id}/resume", {})
    return str(data)


# ─── Home Assistant direct tools ────────────────────────────────────────────


def _require_ha() -> str | None:
    if not HA_URL or not HA_TOKEN:
        return "Home Assistant is not configured. Set HA_URL and HA_TOKEN env vars."
    return None


@mcp.tool()
async def ha_list_entities(domain: str = "") -> str:
    """
    List Home Assistant entities. Filter by domain (e.g. "light", "sensor",
    "switch", "climate") or pass "" for all. Returns up to 100 entities.
    """
    err = _require_ha()
    if err:
        return err
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{HA_URL}/api/states", headers=_ha_headers()) as resp:
                if resp.status != 200:
                    return f"HA error {resp.status}: {await resp.text()}"
                states = await resp.json()
    except aiohttp.ClientConnectorError:
        return f"Cannot connect to Home Assistant at {HA_URL}."
    except Exception as exc:
        return f"HA request failed: {exc}"
    if domain:
        states = [s for s in states if s["entity_id"].startswith(f"{domain}.")]
    if not states:
        return "No entities found" + (f" for domain '{domain}'." if domain else ".")
    states = sorted(states, key=lambda s: s["entity_id"])[:100]
    lines = [
        f"{s['entity_id']:45s}  {s['state']:10s}  {s.get('attributes', {}).get('friendly_name', '')}"
        for s in states
    ]
    return "\n".join(lines)


@mcp.tool()
async def ha_get_state(entity_id: str) -> str:
    """Get the current state and attributes of a Home Assistant entity."""
    err = _require_ha()
    if err:
        return err
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HA_URL}/api/states/{entity_id}", headers=_ha_headers()
            ) as resp:
                if resp.status == 404:
                    return f"Entity '{entity_id}' not found."
                if resp.status != 200:
                    return f"HA error {resp.status}: {await resp.text()}"
                return json.dumps(await resp.json(), indent=2)
    except aiohttp.ClientConnectorError:
        return f"Cannot connect to Home Assistant at {HA_URL}."
    except Exception as exc:
        return f"HA request failed: {exc}"


@mcp.tool()
async def ha_call_service(
    domain: str,
    service: str,
    entity_id: str = "",
    data_json: str = "{}",
) -> str:
    """
    Call a Home Assistant service (e.g. turn on a light, set thermostat).

    Examples:
      ha_call_service("light", "turn_on", "light.kitchen")
      ha_call_service("light", "turn_on", "light.kitchen", '{"brightness": 128}')
      ha_call_service("climate", "set_temperature", "climate.bedroom", '{"temperature": 20}')

    data_json is a JSON string of extra service data (optional).
    """
    err = _require_ha()
    if err:
        return err
    try:
        extra = json.loads(data_json) if data_json else {}
    except json.JSONDecodeError as e:
        return f"Invalid data_json: {e}"
    if not isinstance(extra, dict):
        return "data_json must encode a JSON object."
    payload = dict(extra)
    if entity_id:
        payload["entity_id"] = entity_id
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{HA_URL}/api/services/{domain}/{service}",
                json=payload,
                headers=_ha_headers(),
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    return f"HA error {resp.status}: {text}"
                return f"OK — {domain}.{service} called on {entity_id or '(no target)'}"
    except aiohttp.ClientConnectorError:
        return f"Cannot connect to Home Assistant at {HA_URL}."
    except Exception as exc:
        return f"HA request failed: {exc}"


# ─── Resources ──────────────────────────────────────────────────────────────


@mcp.resource("wactorz://agents")
async def agents_resource() -> str:
    """Current list of running agents as JSON."""
    agents = await _wactorz_get("/agents")
    return json.dumps(agents, indent=2)


@mcp.resource("wactorz://capabilities")
async def capabilities_resource() -> str:
    """Full capability catalog (running + spawnable agents)."""
    data = await _wactorz_post("/chat", {"message": "/capabilities"})
    return str(data.get("response", data))


@mcp.resource("wactorz://ha-map")
async def ha_map_resource() -> str:
    """Home Assistant entity + location map snapshot (from home-assistant-map-agent)."""
    data = await _wactorz_get("/ha-map")
    return json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data)


@mcp.resource("wactorz://config")
async def config_resource() -> str:
    """MCP server configuration (sanitized — no tokens)."""
    return json.dumps(
        {
            "wactorz_url": WACTORZ_URL,
            "wactorz_auth": bool(WACTORZ_API_KEY),
            "ha_url": HA_URL or None,
            "ha_auth": bool(HA_TOKEN),
            "calendar_mcp_url": CALENDAR_MCP_URL or None,
            "calendar_mcp_auth": bool(
                CALENDAR_MCP_CLIENT_ID
                or CALENDAR_MCP_TOKEN
                or CALENDAR_MCP_AUTHORIZATION
            ),
            "calendar_mcp_oauth_client": bool(CALENDAR_MCP_CLIENT_ID),
        },
        indent=2,
    )


# ─── Entry point ────────────────────────────────────────────────────────────


def main() -> None:
    """Console script entry point — runs the MCP server over stdio."""
    logging.basicConfig(level=logging.INFO)
    mcp.run()


if __name__ == "__main__":
    main()
