"""
wactorz/mcp/ha_server.py — MCP server for Home Assistant.

Exposes Home Assistant device/entity/state/automation tools via the
Model Context Protocol so any MCP-capable client (Claude Desktop, etc.)
can query and control your smart-home setup.

Usage::

    wactorz-mcp-ha

Environment variables:
    HA_URL    Home Assistant base URL  (default: http://homeassistant.local:8123)
    HA_TOKEN  Long-lived access token  (required)
"""

from __future__ import annotations

import json
import os
from typing import Any

try:
    import aiohttp
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "MCP dependencies are not installed. "
        "Run: pip install wactorz[mcp]"
    ) from exc

from wactorz.core.integrations.home_assistant.ha_helper import (
    fetch_devices_entities_with_location,
    get_areas,
    get_entities_simple,
    get_automations,
    create_automation_via_rest,
    update_automation,
    delete_automation,
    normalize_ha_ws_url,
    normalize_ha_base_url,
)
from wactorz.core.integrations.home_assistant.ha_web_socket_client import (
    HAWebSocketClient,
)

# ── Config ────────────────────────────────────────────────────────────────────

def _ha_url() -> str:
    return os.environ.get("HA_URL", "http://homeassistant.local:8123")


def _ha_token() -> str:
    token = os.environ.get("HA_TOKEN", "")
    if not token:
        raise RuntimeError("HA_TOKEN environment variable is required.")
    return token


def _rest_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_ha_token()}",
        "Content-Type": "application/json",
    }


_TIMEOUT = aiohttp.ClientTimeout(total=20)

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "wactorz-ha",
    instructions=(
        "Home Assistant integration. Use get_devices to list smart-home devices "
        "with their areas and entities, get_states to read current entity states, "
        "call_service to control devices (e.g. turn lights on/off), and the "
        "automation tools to list, create, update, or delete automations."
    ),
)

# ── Tools — discovery ─────────────────────────────────────────────────────────

@mcp.tool()
async def get_devices() -> str:
    """List all Home Assistant devices with their area, manufacturer, model,
    and associated entities.

    Returns:
        JSON array of device objects.
    """
    try:
        ws_url = normalize_ha_ws_url(_ha_url())
        devices = await fetch_devices_entities_with_location(
            ws_url, _ha_token(), include_states=True
        )
        return json.dumps(devices, indent=2)
    except Exception as exc:
        return f"Error fetching devices: {exc}"


@mcp.tool()
async def get_areas() -> str:
    """List all areas (rooms) configured in Home Assistant.

    Returns:
        JSON array of area objects with area_id and name.
    """
    try:
        ws_url = normalize_ha_ws_url(_ha_url())
        areas = await get_areas(ws_url, _ha_token())
        return json.dumps(areas, indent=2)
    except Exception as exc:
        return f"Error fetching areas: {exc}"


@mcp.tool()
async def get_entities() -> str:
    """List all entities registered in Home Assistant (simplified view).

    Returns:
        JSON array with entity_id, platform, original_name, and name.
    """
    try:
        ws_url = normalize_ha_ws_url(_ha_url())
        entities = await get_entities_simple(ws_url, _ha_token())
        return json.dumps(entities, indent=2)
    except Exception as exc:
        return f"Error fetching entities: {exc}"


@mcp.tool()
async def get_states(entity_id: str = "") -> str:
    """Get the current state of one entity, or all entity states.

    Args:
        entity_id: Optional entity ID to filter (e.g. 'light.living_room').
                   Leave empty to get all states.

    Returns:
        JSON object (single entity) or JSON array (all entities).
    """
    base = normalize_ha_base_url(_ha_url())
    url = f"{base}/api/states"
    if entity_id.strip():
        url = f"{url}/{entity_id.strip()}"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, headers=_rest_headers()) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    return f"Error HTTP {resp.status}: {body[:300]}"
                data: Any = await resp.json()
        return json.dumps(data, indent=2)
    except Exception as exc:
        return f"Error fetching states: {exc}"


# ── Tools — control ───────────────────────────────────────────────────────────

@mcp.tool()
async def call_service(domain: str, service: str, service_data: str = "{}") -> str:
    """Call a Home Assistant service to control a device or trigger an action.

    Common examples:
    - Turn a light on:   domain='light', service='turn_on', service_data='{"entity_id":"light.living_room"}'
    - Turn a switch off: domain='switch', service='turn_off', service_data='{"entity_id":"switch.fan"}'
    - Run a script:      domain='script', service='my_script_name', service_data='{}'

    Args:
        domain:       HA service domain (e.g. 'light', 'switch', 'climate', 'script').
        service:      Service name (e.g. 'turn_on', 'turn_off', 'toggle', 'set_temperature').
        service_data: JSON string of service call parameters.

    Returns:
        JSON array of affected entity states, or an error message.
    """
    base = normalize_ha_base_url(_ha_url())
    url = f"{base}/api/services/{domain}/{service}"

    try:
        payload: dict[str, Any] = json.loads(service_data) if service_data.strip() else {}
    except json.JSONDecodeError as exc:
        return f"Invalid service_data JSON: {exc}"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, headers=_rest_headers(), json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return f"Error HTTP {resp.status}: {body[:300]}"
                data: Any = await resp.json()
        return json.dumps(data, indent=2)
    except Exception as exc:
        return f"Service call failed: {exc}"


@mcp.tool()
async def fire_event(event_type: str, event_data: str = "{}") -> str:
    """Fire a custom event in Home Assistant.

    Args:
        event_type: Event type string (e.g. 'my_custom_event').
        event_data: JSON string of event data payload.

    Returns:
        Confirmation message or an error.
    """
    base = normalize_ha_base_url(_ha_url())
    url = f"{base}/api/events/{event_type}"

    try:
        payload: dict[str, Any] = json.loads(event_data) if event_data.strip() else {}
    except json.JSONDecodeError as exc:
        return f"Invalid event_data JSON: {exc}"

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, headers=_rest_headers(), json=payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    return f"Error HTTP {resp.status}: {body[:300]}"
                data: Any = await resp.json()
        return json.dumps(data, indent=2)
    except Exception as exc:
        return f"Fire event failed: {exc}"


# ── Tools — automations ───────────────────────────────────────────────────────

@mcp.tool()
async def list_automations() -> str:
    """List all automations defined in Home Assistant.

    Returns:
        JSON array of automation config objects.
    """
    try:
        automations = await get_automations(_ha_url(), _ha_token())
        return json.dumps(automations, indent=2)
    except Exception as exc:
        return f"Error fetching automations: {exc}"


@mcp.tool()
async def create_automation(automation_config: str) -> str:
    """Create a new Home Assistant automation.

    Args:
        automation_config: JSON string describing the automation. Required fields:
            - name (str): Human-readable name.
            - trigger (list): List of trigger objects.
            - action (list): List of action objects.
            Optional fields: description, condition, mode ('single'|'parallel'|'queued'|'restart').

    Example::

        {
          "name": "Turn off lights at midnight",
          "trigger": [{"platform": "time", "at": "00:00:00"}],
          "condition": [],
          "action": [{"service": "light.turn_off", "target": {"area_id": "living_room"}}],
          "mode": "single"
        }

    Returns:
        JSON object with automation_id and status, or an error message.
    """
    try:
        config: dict[str, Any] = json.loads(automation_config)
    except json.JSONDecodeError as exc:
        return f"Invalid automation_config JSON: {exc}"

    try:
        result = await create_automation_via_rest(_ha_url(), _ha_token(), config)
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error creating automation: {exc}"


@mcp.tool()
async def update_automation_tool(automation_id: str, automation_config: str) -> str:
    """Update an existing Home Assistant automation.

    Args:
        automation_id:     The automation's internal ID (from list_automations).
        automation_config: JSON string with the updated automation config.
                           Same structure as create_automation.

    Returns:
        JSON object with status, or an error message.
    """
    try:
        config: dict[str, Any] = json.loads(automation_config)
    except json.JSONDecodeError as exc:
        return f"Invalid automation_config JSON: {exc}"

    try:
        result = await update_automation(_ha_url(), _ha_token(), automation_id, config)
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"Error updating automation: {exc}"


@mcp.tool()
async def delete_automation_tool(automation_id: str) -> str:
    """Delete an existing Home Assistant automation.

    Args:
        automation_id: The automation's internal ID (from list_automations).

    Returns:
        'deleted' on success, or an error message.
    """
    try:
        success = await delete_automation(_ha_url(), _ha_token(), automation_id)
        return "deleted" if success else f"Deletion failed for automation '{automation_id}'"
    except Exception as exc:
        return f"Error deleting automation: {exc}"


# ── Tools — websocket / raw ───────────────────────────────────────────────────

@mcp.tool()
async def websocket_call(ws_type: str, params: str = "{}") -> str:
    """Execute a raw Home Assistant WebSocket command.

    Useful for registry calls or commands not covered by other tools.
    See the HA WebSocket API docs for available types.

    Args:
        ws_type: WebSocket command type
                 (e.g. 'config/area_registry/list', 'get_states').
        params:  JSON string of additional parameters for the command.

    Returns:
        JSON result from Home Assistant, or an error message.
    """
    try:
        extra: dict[str, Any] = json.loads(params) if params.strip() else {}
    except json.JSONDecodeError as exc:
        return f"Invalid params JSON: {exc}"

    ws_url = normalize_ha_ws_url(_ha_url())
    try:
        async with HAWebSocketClient(ws_url, _ha_token()) as ha:
            result = await ha.call(ws_type, **extra)
        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"WebSocket call failed: {exc}"


# ── Entry point ───────────────────────────────────────────────────────────────

def cli_main() -> None:
    """Sync entry point for the ``wactorz-mcp-ha`` console script."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    cli_main()
