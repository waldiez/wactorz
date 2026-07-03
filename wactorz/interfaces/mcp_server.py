"""MCP Server - Exposes wactorz capabilities as Model Context Protocol tools.

Any MCP-compatible client (Claude Desktop, Cursor, Zed, etc.) can connect
and drive a running wactorz instance: chat with the orchestrator, manage
live agents, and control Home Assistant entities directly.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any

import aiohttp
from wactorz.core.integrations.google_calendar_mcp import (
    DEFAULT_CALENDAR_MCP_SCOPES,
    GOOGLE_CALENDAR_MCP_URL,
    GoogleCalendarMcpClient,
)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:
    raise SystemExit(
        "The 'mcp' package is required. Install with: pip install wactorz[mcp]"
    ) from exc

logger = logging.getLogger(__name__)

WACTORZ_URL = os.getenv("WACTORZ_URL", "http://localhost:8000").rstrip("/")
WACTORZ_API_KEY = os.getenv("WACTORZ_API_KEY", "")
HA_URL = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
CALENDAR_MCP_URL = os.getenv("CALENDAR_MCP_URL", GOOGLE_CALENDAR_MCP_URL).rstrip("/")
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
    DEFAULT_CALENDAR_MCP_SCOPES,
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
            async with session.delete(f"{WACTORZ_URL}{path}", headers=_wactorz_headers()) as resp:
                return {"status": resp.status, "text": await resp.text()}
    except aiohttp.ClientConnectorError:
        return {"status": 0, "text": f"Cannot connect to wactorz at {WACTORZ_URL}. Is it running?"}
    except Exception as exc:
        return {"status": 0, "text": str(exc)}


# ─── Orchestrator tools ─────────────────────────────────────────────────────


@mcp.tool()
async def ask_wactorz(message: str) -> str:
    """Send a message to the wactorz main orchestrator.

    The orchestrator will route to an existing agent, spawn a new one, or
    answer directly. Use for fuzzy requests ("monitor my CPU temp",
    "what's the weather") where you don't know which agent should handle it.
    """
    data = await _wactorz_post("/chat", {"message": message})
    return str(data.get("response", data))


@mcp.tool()
async def ask_agent(agent_name: str, message: str) -> str:
    """Send a message directly to a named agent, bypassing the orchestrator.

    Use when you already know which agent should handle the request.
    Agent names are lowercase-hyphenated (e.g. "home-assistant-agent").
    Run list_agents() first if unsure.
    """
    data = await _wactorz_post("/chat", {"message": message, "agent_name": agent_name})
    return str(data.get("response", data))


# ─── Remote Calendar MCP tools ──────────────────────────────────────────────


def _calendar_timezone() -> tuple[Any, str | None]:
    """Return ``(tzinfo, iana_name)`` for calendar math.

    ``iana_name`` is only set when it's a zone we can hand to the Google Calendar
    API; otherwise it's ``None`` and callers omit ``timeZone`` (the offset-aware
    ISO timestamps already pin the window). Keeps the tools working on systems
    without the IANA tz database (e.g. Windows without ``tzdata``), where even
    ``ZoneInfo("UTC")`` raises — and the API rejects a bare ``UTC`` key anyway.
    """
    name = os.getenv("CALENDAR_MCP_TIMEZONE") or os.getenv("TZ")
    if name:
        try:
            return ZoneInfo(name), name
        except Exception:
            logger.debug("Unknown calendar timezone %r; using local time", name)
    local = datetime.now().astimezone().tzinfo or timezone.utc
    return local, getattr(local, "key", None)


def _calendar_range_arguments(range_name: str, count: int = 10) -> dict[str, Any]:
    tz, tz_name = _calendar_timezone()
    now = datetime.now(tz)
    if range_name == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif range_name == "week":
        start = now
        end = now + timedelta(days=7)
    else:
        start = now
        end = now + timedelta(days=1)
    args = {
        "pageSize": count,
        "orderBy": "startTime",
        "startTime": start.isoformat(),
        "endTime": end.isoformat(),
    }
    if tz_name:
        args["timeZone"] = tz_name
    return args


def _calendar_mcp_status_from_constants() -> dict[str, Any]:
    return {
        "calendar_mcp_url": CALENDAR_MCP_URL or None,
        "calendar_mcp_auth": bool(
            CALENDAR_MCP_CLIENT_ID
            or CALENDAR_MCP_TOKEN
            or CALENDAR_MCP_AUTHORIZATION
        ),
        "calendar_mcp_oauth_client": bool(CALENDAR_MCP_CLIENT_ID),
        "calendar_mcp_redirect_uri": CALENDAR_MCP_REDIRECT_URI if CALENDAR_MCP_CLIENT_ID else None,
        "calendar_mcp_token_file": CALENDAR_MCP_TOKEN_FILE if CALENDAR_MCP_CLIENT_ID else None,
    }


async def _calendar_mcp_call(tool_name: str, arguments: dict | None = None) -> str:
    """Call a tool on the configured Google Calendar MCP server."""
    return await GoogleCalendarMcpClient().call_tool(tool_name, arguments)


@mcp.tool()
async def calendar_status() -> str:
    """Show sanitized Google Calendar MCP configuration status."""
    status = _calendar_mcp_status_from_constants()
    return json.dumps(status, indent=2)


@mcp.tool()
async def calendar_mcp_list_tools() -> str:
    """List tools exposed by the configured Google Calendar MCP server."""
    return await GoogleCalendarMcpClient().list_tools()


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
    """List upcoming Google Calendar events through the Google Calendar MCP server."""
    return await _calendar_mcp_call("list_events", {"pageSize": count, "orderBy": "startTime"})


@mcp.tool()
async def calendar_today() -> str:
    """List today's Google Calendar events through the Google Calendar MCP server."""
    return await _calendar_mcp_call("list_events", _calendar_range_arguments("today"))


@mcp.tool()
async def calendar_week() -> str:
    """List this week's Google Calendar events through the Google Calendar MCP server."""
    return await _calendar_mcp_call("list_events", _calendar_range_arguments("week"))


@mcp.tool()
async def calendar_create_event(
    summary: str,
    start: str,
    end: str = "",
    location: str = "",
    description: str = "",
) -> str:
    """Create a Google Calendar event through the Google Calendar MCP server."""
    if not end:
        return "calendar_create_event requires an ISO-8601 end time because Google Calendar MCP create_event requires endTime."
    payload = {"summary": summary, "startTime": start, "endTime": end}
    if location:
        payload["location"] = location
    if description:
        payload["description"] = description
    return await _calendar_mcp_call("create_event", payload)


@mcp.tool()
async def calendar_delete_event(event_id: str) -> str:
    """Delete a Google Calendar event through the Google Calendar MCP server."""
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
    """List the full agent capability catalog (running + spawnable).

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
    """List Home Assistant entities. Filter by domain (e.g. "light", "sensor",
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
    """Call a Home Assistant service (e.g. turn on a light, set thermostat).

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
            **_calendar_mcp_status_from_constants(),
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
