from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
import re
import time

import aiohttp

from .ha_web_socket_client import HAWebSocketClient


async def get_automations(base_url: str, token: str) -> list[dict[str, Any]]:
    """
    Fetch all automations from Home Assistant and return a rich JSON list.
    """
    ws_url = normalize_ha_ws_url(base_url)
    rest_url = normalize_ha_base_url(base_url)

    async with HAWebSocketClient(ws_url, token) as ha:
        states = await ha.call("get_states")
        automation_ids: list[str] = []
        for s in (states or []):
            if not isinstance(s, dict):
                continue
            entity_id = s.get("entity_id")
            if isinstance(entity_id, str) and entity_id.startswith("automation."):
                automation_ids.append(s.get("attributes")["id"] if isinstance(s.get("attributes"), dict) else "")

        automations: list[dict[str, Any]] = []
        for id in automation_ids:
            config = None
            if isinstance(id, str) and id.strip():
                config = await _fetch_automation_config(rest_url, id.strip(), token)
                automations.append(config)


        return automations or []


async def delete_automation(base_url: str, token: str, automation_id: str) -> bool:
    """Delete an automation by ID. Returns True if deletion was successful.
    This is undocumented but that is the endpoint used by the HA frontend to delete automations, 
    so it should be stable."""
    normalized_base = normalize_ha_base_url(base_url)
    endpoint = f"{normalized_base}/api/config/automation/config/{automation_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.delete(endpoint, headers=headers) as response:
            print(f"response.status: {response.status}")
            return response.status == 200


async def fetch_devices_entities_with_location(
    ws_url: str,
    token: str,
    include_states: bool = False,
) -> List[Dict[str, Any]]:
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        # Core registries
        areas = await ha.call("config/area_registry/list")
        devices = await ha.call("config/device_registry/list")
        entities = await ha.call("config/entity_registry/list")
        states = await ha.call("get_states") if include_states else []

        area_name_by_id = {a["area_id"]: a.get("name") for a in areas}

        # Build a state lookup by entity_id when states are requested
        states_by_entity_id: Dict[str, Dict[str, Any]] = (
            {s["entity_id"]: s for s in states} if include_states else {}
        )

        # Group entities by device_id
        entities_by_device: Dict[str, List[Dict[str, Any]]] = {}
        for e in entities:
            device_id = e.get("device_id")
            if not device_id:
                continue
            entities_by_device.setdefault(device_id, []).append(e)

        output: List[Dict[str, Any]] = []
        for d in devices:
            device_id = d["id"]
            # device "location" is area_id on the device registry entry (if set)
            device_area_id: Optional[str] = d.get("area_id")
            device_area_name = area_name_by_id.get(device_area_id) if device_area_id else None

            ents = []
            for e in entities_by_device.get(device_id, []):
                # entity can also have its own area_id in the entity registry
                entity_area_id = e.get("area_id") or device_area_id
                entity_area_name = area_name_by_id.get(entity_area_id) if entity_area_id else None

                entity_entry: Dict[str, Any] = {
                    "entity_id": e.get("entity_id"),
                    "unique_id": e.get("unique_id"),
                    "platform": e.get("platform"),
                    "area": entity_area_name,
                    # "disabled_by": e.get("disabled_by"),
                    # "hidden_by": e.get("hidden_by"),
                    "original_name": e.get("original_name"),
                    "name": e.get("name"),
                }

                if include_states:
                    state_data = states_by_entity_id.get(e.get("entity_id", ""), {})
                    entity_entry["state"] = state_data.get("state")
                    entity_entry["attributes"] = state_data.get("attributes", {})

                ents.append(entity_entry)

            output.append(
                {
                    "device_id": device_id,
                    "name": d.get("name_by_user") or d.get("name"),
                    "manufacturer": d.get("manufacturer"),
                    "model": d.get("model"),
                    # "sw_version": d.get("sw_version"),
                    # "hw_version": d.get("hw_version"),
                    "area": device_area_name,
                    "entities": sorted(ents, key=lambda x: (x["entity_id"] or "")),
                }
            )

        return sorted(output, key=lambda x: (x["name"] or ""))


def normalize_ha_ws_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()

    if scheme in {"ws", "wss"}:
        if raw.endswith("/api/websocket"):
            return raw
        return f"{raw}/api/websocket"

    if scheme in {"http", "https"}:
        ws_scheme = "wss" if scheme == "https" else "ws"
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/api/websocket"):
            new_path = path
        elif path:
            new_path = f"{path}/api/websocket"
        else:
            new_path = "/api/websocket"
        netloc = parsed.netloc or parsed.path
        return f"{ws_scheme}://{netloc}{new_path}"

    return raw


def normalize_ha_base_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()

    if scheme in {"http", "https"}:
        netloc = parsed.netloc or parsed.path
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/api/websocket"):
            path = path[: -len("/api/websocket")]
        return f"{scheme}://{netloc}{path}"

    if scheme in {"ws", "wss"}:
        http_scheme = "https" if scheme == "wss" else "http"
        netloc = parsed.netloc or parsed.path
        path = (parsed.path or "").rstrip("/")
        if path.endswith("/api/websocket"):
            path = path[: -len("/api/websocket")]
        return f"{http_scheme}://{netloc}{path}"

    return raw


def extract_entity_ids(devices: List[Dict[str, Any]]) -> List[str]:
    entity_ids: List[str] = []
    seen: set[str] = set()
    for device in devices:
        for entity in device.get("entities", []) or []:
            entity_id = str(entity.get("entity_id", "")).strip()
            if not entity_id or entity_id in seen:
                continue
            seen.add(entity_id)
            entity_ids.append(entity_id)
    return entity_ids


async def create_automation_via_websocket(
    ws_url: str,
    token: str,
    automation_config: Dict[str, Any],
) -> Dict[str, Any]:
    return await create_automation_via_rest(ws_url, token, automation_config)


async def _fetch_automation_config(rest_base: str, automation_id: str, token: str) -> dict[str, Any] | None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    url = f"{rest_base}/api/config/automation/config/{automation_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data if isinstance(data, dict) else None
        except (aiohttp.ClientError, ValueError):
            return None


async def create_automation_via_rest(
    base_url: str,
    token: str,
    automation_config: Dict[str, Any],
) -> Dict[str, Any]:
    normalized_base = normalize_ha_base_url(base_url)
    alias = str(automation_config.get("name") or "Generated automation").strip()
    description = str(automation_config.get("description") or "").strip()
    trigger = automation_config.get("trigger") or []
    condition = automation_config.get("condition") or []
    action = automation_config.get("action") or []
    mode = str(automation_config.get("mode") or "single").strip() or "single"

    slug_base = re.sub(r"[^a-z0-9]+", "_", alias.lower()).strip("_") or "generated_automation"
    automation_id = f"{slug_base}_{int(time.time())}"

    payload = {
        "alias": alias,
        "description": description,
        "trigger": trigger,
        "condition": condition,
        "action": action,
        "mode": mode,
    }

    endpoint = f"{normalized_base}/api/config/automation/config/{automation_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, headers=headers, json=payload) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            body: Any
            if "application/json" in content_type:
                body = await response.json()
            else:
                body = await response.text()

            if response.status >= 400:
                raise RuntimeError(
                    f"REST automation create failed ({response.status}): {body}"
                )

            return {
                "automation_id": automation_id,
                "status": response.status,
                "result": body,
            }
