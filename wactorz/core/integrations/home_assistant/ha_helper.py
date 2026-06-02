from __future__ import annotations

from typing import Any
from urllib.parse import urlparse
import hashlib
import re
import time

import aiohttp

from .ha_web_socket_client import HAWebSocketClient


# ── SWID ────────────────────────────────────────────────────────
def _normalize_swid_segment(text: str) -> str:
    """Normalize a text segment for use in a SWID path.

    Lowercases, replaces whitespace/underscores with hyphens,
    strips unsafe characters, and collapses repeated hyphens.
    """
    segment = text.strip().lower()
    # Replace whitespace and underscores with hyphens
    segment = re.sub(r"[\s_]+", "-", segment)
    # Keep only alphanumeric, hyphens, and dots
    segment = re.sub(r"[^a-z0-9\-\.]", "", segment)
    # Collapse repeated hyphens and strip leading/trailing hyphens
    segment = re.sub(r"-{2,}", "-", segment).strip("-")
    return segment


def generate_swid(
    device_id: str,
    name: str | None = None,
    area: str | None = None,
) -> str:
    """Generate an internal did:swid: identifier for a Home Assistant device.

    This is a project-specific identifier in ``did:swid:`` format, designed
    to be spatial and human-readable.  It is NOT an official Spatial Web DID
    implementation — treat it as a stable internal label that can be replaced
    later if an official method becomes available.

    Format:  did:swid:home:<area>:<device-name>-<short-hash>

    * *area* — normalised area/room name, or ``unassigned`` when unknown.
    * *device-name* — normalised device display name, or ``device`` as fallback.
    * *short-hash* — first 6 hex chars of the SHA-256 of the stable HA device
      registry ``id``, appended to guarantee uniqueness even if two devices
      share the same human-readable path.

    The value is deterministic: the same inputs always produce the same SWID.
    """
    # --- area segment ---
    area_segment = _normalize_swid_segment(area) if area else ""
    if not area_segment:
        area_segment = "unassigned"

    # --- device-name segment ---
    name_segment = _normalize_swid_segment(name) if name else ""
    if not name_segment:
        name_segment = "device"

    # --- short deterministic hash from the stable device registry id ---
    stable_hash = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:6]

    return f"did:swid:home:{area_segment}:{name_segment}-{stable_hash}"


# ── URL Helpers ────────────────────────────────────────────────────────
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


# ── HA Data ────────────────────────────────────────────────────────
async def fetch_devices_entities_with_location(
    ws_url: str,
    token: str,
    include_states: bool = False,
) -> list[dict[str, Any]]:
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        # Core registries
        areas = await ha.call("config/area_registry/list")
        devices = await ha.call("config/device_registry/list")
        entities = await ha.call("config/entity_registry/list")
        states = await ha.call("get_states") if include_states else []

        area_name_by_id = {a["area_id"]: a.get("name") for a in areas}

        # Build a state lookup by entity_id when states are requested
        states_by_entity_id: dict[str, dict[str, Any]] = (
            {s["entity_id"]: s for s in states} if include_states else {}
        )

        # Group entities by device_id
        entities_by_device: dict[str, list[dict[str, Any]]] = {}
        for e in entities:
            device_id = e.get("device_id")
            if not device_id:
                continue
            entities_by_device.setdefault(device_id, []).append(e)

        output: list[dict[str, Any]] = []
        for d in devices:
            device_id = d["id"]
            # device "location" is area_id on the device registry entry (if set)
            device_area_id: str | None = d.get("area_id")
            device_area_name = area_name_by_id.get(device_area_id) if device_area_id else None

            ents = []
            for e in entities_by_device.get(device_id, []):
                # entity can also have its own area_id in the entity registry
                entity_area_id = e.get("area_id") or device_area_id
                entity_area_name = area_name_by_id.get(entity_area_id) if entity_area_id else None

                entity_entry: dict[str, Any] = {
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

            device_name = d.get("name_by_user") or d.get("name")
            output.append(
                {
                    "device_id": device_id,
                    "name": device_name,
                    "swid": generate_swid(device_id, name=device_name, area=device_area_name),
                    "manufacturer": d.get("manufacturer"),
                    "model": d.get("model"),
                    # "sw_version": d.get("sw_version"),
                    # "hw_version": d.get("hw_version"),
                    "area": device_area_name,
                    "entities": sorted(ents, key=lambda x: (x["entity_id"] or "")),
                }
            )

        return sorted(output, key=lambda x: (x["name"] or ""))


async def _get_floors_safe(ha: HAWebSocketClient) -> list[dict[str, Any]]:
    """Fetch the floor registry within an open HAWebSocketClient, returning [] on older HA versions."""
    try:
        return await ha.call("config/floor_registry/list") or []
    except Exception:
        return []


async def get_full_ha_data(ws_url: str, token: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch raw registry dumps for floors, areas, devices, entities, and states.

    Returns a dict with the following keys, each containing the full unmodified
    list as returned by the Home Assistant WebSocket API:

    - ``floors``  — floor registry entries (empty list on older HA versions that
      do not support ``config/floor_registry/list``).  Each entry includes:
      ``floor_id``, ``name``, and optionally ``aliases``, ``icon``, ``level``,
      ``created_at``, ``modified_at``.

    - ``areas``   — area registry entries.  Each entry includes: ``area_id``,
      ``name``, ``floor_id``, ``aliases``, ``labels``, ``icon``, ``picture``,
      ``humidity_entity_id``, ``temperature_entity_id``, ``created_at``,
      ``modified_at``.

    - ``devices`` — device registry entries.  Each entry includes: ``id``,
      ``name``, ``name_by_user``, ``area_id``, ``manufacturer``, ``model``,
      ``model_id``, ``sw_version``, ``hw_version``, ``serial_number``,
      ``config_entries``, ``config_entries_subentries``, ``connections``,
      ``identifiers``, ``labels``, ``disabled_by``, ``entry_type``,
      ``primary_config_entry``, ``via_device_id``, ``created_at``,
      ``modified_at``.

    - ``entities`` — entity registry entries.  Each entry includes:
      ``entity_id``, ``unique_id``, ``platform``, ``device_id``,
      ``config_entry_id``, ``config_subentry_id``, ``area_id``, ``name``,
      ``original_name``, ``icon``, ``entity_category``, ``translation_key``,
      ``has_entity_name``, ``disabled_by``, ``hidden_by``, ``labels``,
      ``categories``, ``options``, ``id``, ``created_at``, ``modified_at``.

    - ``states``  — current entity states from ``get_states``.  Each entry
      includes: ``entity_id``, ``state``, ``attributes``, ``last_changed``,
      ``last_reported``, ``last_updated``, ``context``.
    """
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        floors = await _get_floors_safe(ha)
        areas = await ha.call("config/area_registry/list")
        devices = await ha.call("config/device_registry/list")
        entities = await ha.call("config/entity_registry/list")
        states = await ha.call("get_states")

        return {
            "floors": floors,
            "areas": areas or [],
            "devices": devices or [],
            "entities": entities or [],
            "states": states or [],
        }


async def get_simplified_ha_data(ws_url: str, token: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch compact, null-stripped registry data for floors, areas, devices, and entities.

    Unlike :func:`get_full_ha_data`, this function trims each registry entry to
    only its most useful fields, drops ``None``/empty-list values (and ``icon``/
    ``entity_picture`` keys), and resolves entity display names from live states.
    ``hassio`` platform entities are excluded from the entity list.

    States are fetched internally to resolve friendly names and current state
    values, but are **not** included as a top-level key in the returned dict.

    Returns a dict with the following keys:

    - ``floors``  — one entry per floor: ``floor_id``, ``name``.  Empty list on
      older HA versions without floor registry support.

    - ``areas``   — one entry per area: ``area_id``, ``name``.

    - ``devices`` — one entry per device (null-value fields omitted):
      ``id``, ``name``, ``name_by_user``, ``area_id``, ``manufacturer``,
      ``model``, ``labels``, ``disabled_by``.

    - ``entities`` — one entry per non-hassio entity (null-value fields omitted):
      ``entity_id``, ``domain``, ``name`` (resolved from friendly_name →
      original_name → name), ``area_id`` (falls back to device area),
      ``device_id``, ``disabled_by``, ``hidden_by``, ``entity_category``,
      ``platform``, ``state``, plus any extra state attributes (excluding
      ``friendly_name``).
    """
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        floors = await _get_floors_safe(ha)
        areas = await ha.call("config/area_registry/list") or []
        devices = await ha.call("config/device_registry/list") or []
        entities = await ha.call("config/entity_registry/list") or []
        states = await ha.call("get_states") or []

        states_by_id = {s["entity_id"]: s for s in states}
        device_area_by_id = {d["id"]: d.get("area_id") for d in devices}

        _SKIP_KEYS = {"icon", "entity_picture"}

        def drop_nulls(d: dict) -> dict:
            return {k: v for k, v in d.items() if v is not None and v != [] and k not in _SKIP_KEYS}

        return {
            "floors": [
                drop_nulls({"floor_id": f["floor_id"], "name": f.get("name")})
                for f in floors
            ],
            "areas": [
                drop_nulls({"area_id": a["area_id"], "name": a.get("name")})
                for a in areas
            ],
            "devices": [
                drop_nulls({
                    "id": d["id"],
                    "name": d.get("name"),
                    "name_by_user": d.get("name_by_user"),
                    "area_id": d.get("area_id"),
                    "manufacturer": d.get("manufacturer"),
                    "model": d.get("model"),
                    "labels": d.get("labels", []),
                    "disabled_by": d.get("disabled_by"),
                })
                for d in devices
            ],
            "entities": [
                drop_nulls({
                    "entity_id": e.get("entity_id"),
                    "domain": (e.get("entity_id") or "").split(".")[0] or None,
                    "name": (
                        states_by_id.get(e.get("entity_id", ""), {}).get("attributes", {}).get("friendly_name")
                        or e.get("original_name")
                        or e.get("name")
                    ),
                    "area_id": e.get("area_id") or device_area_by_id.get(e.get("device_id")),
                    "device_id": e.get("device_id"),
                    "disabled_by": e.get("disabled_by"),
                    "hidden_by": e.get("hidden_by"),
                    "entity_category": e.get("entity_category"),
                    "platform": e.get("platform"),
                    "state": states_by_id.get(e.get("entity_id", ""), {}).get("state"),
                    # "last_changed": states_by_id.get(e.get("entity_id", ""), {}).get("last_changed"),
                    **{k: v for k, v in states_by_id.get(e.get("entity_id", ""), {}).get("attributes", {}).items() if k != "friendly_name"},
                })
                for e in entities
                if e.get("platform") != "hassio"
            ],
        }


async def get_floors(ws_url: str, token: str) -> list[dict[str, Any]]:
    """Fetch the floor registry and return the list as-is from Home Assistant.

    Each entry in the returned list is a floor registry dict with the following
    fields: ``floor_id``, ``name``, ``level``, ``aliases``, ``icon``,
    ``created_at``, ``modified_at``.  Fields that are not set in HA will be
    present but ``null``.

    Returns an empty list on older HA instances that do not support the floor
    registry WebSocket command (introduced in HA 2024.x).
    """
    ws_url = normalize_ha_ws_url(ws_url)
    try:
        async with HAWebSocketClient(ws_url, token) as ha:
            floors = await ha.call("config/floor_registry/list")
            return floors or []
    except Exception:
        return []


async def get_areas(ws_url: str, token: str) -> list[dict[str, Any]]:
    """Fetch the full area registry and return the list as-is from Home Assistant.

    Each entry in the returned list is an area registry dict with the following
    fields: ``area_id``, ``name``, ``floor_id``, ``aliases``, ``labels``,
    ``icon``, ``picture``, ``humidity_entity_id``, ``temperature_entity_id``,
    ``created_at``, ``modified_at``.  Fields that are not set in HA will be
    present but ``null``.
    """
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        areas = await ha.call("config/area_registry/list")
        return areas or []


async def get_devices(ws_url: str, token: str) -> list[dict[str, Any]]:
    """Fetch the full device registry and return the list as-is from Home Assistant,
    with an additional ``swid`` field appended to each entry.

    Each entry in the returned list is a device registry dict with the following
    fields: ``id``, ``name``, ``name_by_user``, ``area_id``, ``configuration_url``,
    ``config_entries``, ``config_entries_subentries``, ``connections``,
    ``identifiers``, ``labels``, ``manufacturer``, ``model``, ``model_id``,
    ``sw_version``, ``hw_version``, ``serial_number``, ``entry_type``,
    ``primary_config_entry``, ``via_device_id``, ``disabled_by``,
    ``created_at``, ``modified_at``.  Fields that are not set in HA will be
    present but ``null``.

    A ``swid`` key is added by this function (not from HA) containing the
    project-specific ``did:swid:`` identifier generated by :func:`generate_swid`.
    """
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        devices = await ha.call("config/device_registry/list")
        for d in (devices or []):
            d["swid"] = generate_swid(
                d["id"],
                name=d.get("name_by_user") or d.get("name"),
                area=d.get("area_id"),
            )
        return devices or []


async def get_devices_simple(ws_url: str, token: str) -> list[dict[str, Any]]:
    ws_url = normalize_ha_ws_url(ws_url)
    devices = await get_devices(ws_url, token)
    return [
        {
            "device_id": d["id"],
            "name": d.get("name_by_user") or d.get("name"),
            "swid": d.get("swid", ""),
            "manufacturer": d.get("manufacturer"),
            "model": d.get("model"),
        }
        for d in (devices or [])
    ]


async def get_entities(ws_url: str, token: str) -> list[dict[str, Any]]:
    """Fetch the full entity registry and return the list as-is from Home Assistant.

    Each entry in the returned list is an entity registry dict with the following
    fields: ``entity_id``, ``unique_id``, ``id``, ``platform``, ``device_id``,
    ``config_entry_id``, ``config_subentry_id``, ``area_id``, ``name``,
    ``original_name``, ``icon``, ``entity_category``, ``translation_key``,
    ``has_entity_name``, ``disabled_by``, ``hidden_by``, ``labels``,
    ``categories``, ``options``, ``created_at``, ``modified_at``.  Fields
    that are not set in HA will be present but ``null``.
    """
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        entities = await ha.call("config/entity_registry/list")
        return entities or []


async def get_entities_for_display(ws_url: str, token: str) -> dict[str, Any]:
    """Fetch entity registry entries optimised for display, as returned by the HA frontend API.

    Returns a dict with two top-level keys:

    - ``entity_categories`` — a mapping of numeric codes to category names,
      e.g. ``{"0": "config", "1": "diagnostic"}``.  Use this to decode the
      ``ec`` field on each entity entry.

    - ``entities`` — list of compact entity dicts.  Each entry uses
      abbreviated field names to reduce payload size.  Fields that are not set
      are omitted entirely (no ``null`` values):

      - ``ei``  — ``entity_id``
      - ``pl``  — ``platform``
      - ``ai``  — ``area_id``
      - ``lb``  — ``labels``
      - ``di``  — ``device_id``
      - ``ic``  — ``icon``
      - ``tk``  — ``translation_key``
      - ``ec``  — entity category code (integer; look up in ``entity_categories``)
      - ``hb``  — ``hidden_by``
      - ``hn``  — ``has_entity_name``
      - ``en``  — display name (resolved by HA, equivalent to ``friendly_name``)
      - ``dp``  — display precision (decimal places, only present when set)
    """
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        entities = await ha.call("config/entity_registry/list_for_display")
        return entities or {}


async def get_exposed_entities(ws_url: str, token: str) -> dict[str, Any]:
    """Fetch entities exposed to HA assistants and return the response as-is from Home Assistant.

    Returns a dict with a single top-level key:

    - ``exposed_entities`` — a mapping of ``entity_id`` strings to assistant
      exposure dicts.  Each value is a dict whose keys are assistant names
      (e.g. ``"conversation"``) and whose values are booleans indicating
      whether the entity is exposed to that assistant.

    Example::

        {
            "exposed_entities": {
                "light.living_room": {"conversation": true},
                "switch.fan":        {"conversation": false}
            }
        }
    """
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        entities = await ha.call("homeassistant/expose_entity/list")
        return entities or {}


async def get_entities_simple(ws_url: str, token: str) -> list[dict[str, Any]]:
    ws_url = normalize_ha_ws_url(ws_url)
    entities = await get_entities(ws_url, token)
    return [
        {
            "entity_id": e.get("entity_id"),
            "unique_id": e.get("unique_id"),
            "platform": e.get("platform"),
            "original_name": e.get("original_name"),
            "name": e.get("name"),
        }
        for e in (entities or [])
    ]


async def get_states(ws_url: str, token: str) -> list[dict[str, Any]]:
    """Fetch all current entity states and return the list as-is from Home Assistant.

    Each entry in the returned list is a state dict with the following fields:

    - ``entity_id``     — the entity identifier (e.g. ``"light.living_room"``)
    - ``state``         — current state string (e.g. ``"on"``, ``"off"``, ``"22.5"``)
    - ``attributes``    — dict of domain-specific attributes; always includes
      ``friendly_name`` when set; may include ``supported_features``,
      ``device_class``, ``unit_of_measurement``, ``brightness``, etc.
    - ``last_changed``  — ISO 8601 timestamp of last state value change
    - ``last_reported`` — ISO 8601 timestamp of last state report (even if unchanged)
    - ``last_updated``  — ISO 8601 timestamp of last update (state or attributes)
    - ``context``       — dict with ``id``, ``parent_id``, ``user_id``
    """
    ws_url = normalize_ha_ws_url(ws_url)
    async with HAWebSocketClient(ws_url, token) as ha:
        states = await ha.call("get_states")
        return states or []


async def get_config_entries(ws_url: str, token: str) -> list[dict[str, Any]]:
    """Fetch config entries and return the list as-is from Home Assistant.

    Each entry in the returned list is a config entry dict with the following
    fields: ``entry_id``, ``domain``, ``title``, ``source``, ``state``,
    ``supports_options``, ``supports_remove_device``, ``supports_unload``,
    ``supports_reconfigure``, ``supported_subentry_types``,
    ``pref_disable_new_entities``, ``pref_disable_polling``, ``disabled_by``,
    ``reason``, ``error_reason_translation_key``,
    ``error_reason_translation_placeholders``, ``num_subentries``,
    ``created_at``, ``modified_at``.  Fields that are not set in HA will be
    present but ``null``.

    Returns an empty list if the WebSocket command is unavailable.
    """
    ws_url = normalize_ha_ws_url(ws_url)
    try:
        async with HAWebSocketClient(ws_url, token) as ha:
            entries = await ha.call("config_entries/get")
            return entries or []
    except Exception:
        return []


# ── Automation ────────────────────────────────────────────────────────
async def _fetch_automation_config(
    rest_base: str,
    automation_id: str,
    token: str,
    session: aiohttp.ClientSession,
) -> dict[str, Any] | None:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{rest_base}/api/config/automation/config/{automation_id}"
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            return data if isinstance(data, dict) else None
    except (aiohttp.ClientError, ValueError):
        return None


async def _post_automation_config(
    base_url: str,
    token: str,
    automation_id: str,
    automation_config: dict[str, Any],
    default_alias: str = "automation",
) -> dict[str, Any]:
    """POST an automation config to the HA REST API (create or update).

    The HA REST API uses the same POST /api/config/automation/config/{id}
    endpoint for both creation and update; the caller supplies the id.
    Raises RuntimeError on HTTP 4xx/5xx.
    """
    normalized_base = normalize_ha_base_url(base_url)

    alias = str(automation_config.get("name") or default_alias).strip()
    payload = {
        "alias": alias,
        "description": str(automation_config.get("description") or "").strip(),
        "trigger": automation_config.get("trigger") or [],
        "condition": automation_config.get("condition") or [],
        "action": automation_config.get("action") or [],
        "mode": str(automation_config.get("mode") or "single").strip() or "single",
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
                    f"REST automation POST failed ({response.status}): {body}"
                )

            return {
                "automation_id": automation_id,
                "status": response.status,
                "result": body,
            }


async def create_automation_via_websocket(
    ws_url: str,
    token: str,
    automation_config: dict[str, Any],
) -> dict[str, Any]:
    return await create_automation_via_rest(ws_url, token, automation_config)


async def create_automation_via_rest(
    base_url: str,
    token: str,
    automation_config: dict[str, Any],
) -> dict[str, Any]:
    """Create a new automation by generating a unique ID and POSTing to the HA REST API."""
    alias = str(automation_config.get("name") or "Generated automation").strip()
    slug_base = re.sub(r"[^a-z0-9]+", "_", alias.lower()).strip("_") or "generated_automation"
    automation_id = f"{slug_base}_{int(time.time())}"
    return await _post_automation_config(
        base_url, token, automation_id, automation_config, default_alias="Generated automation"
    )


async def get_automations(base_url: str, token: str) -> list[dict[str, Any]]:
    """Fetch all automations from Home Assistant and return a rich JSON list."""
    ws_url = normalize_ha_ws_url(base_url)
    rest_url = normalize_ha_base_url(base_url)

    async with HAWebSocketClient(ws_url, token) as ha:
        states = await ha.call("get_states") or []

    automation_ids: list[str] = []
    for s in states:
        if not isinstance(s, dict):
            continue
        entity_id = s.get("entity_id")
        if isinstance(entity_id, str) and entity_id.startswith("automation."):
            attrs = s.get("attributes")
            if isinstance(attrs, dict) and attrs.get("id"):
                automation_ids.append(attrs["id"])

    automations: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        for automation_id in automation_ids:
            config = await _fetch_automation_config(rest_url, automation_id, token, session)
            if config is not None:
                automations.append(config)

    return automations


async def update_automation(
    base_url: str,
    token: str,
    automation_id: str,
    automation_config: dict[str, Any],
) -> dict[str, Any]:
    """Overwrite an existing automation by POSTing to its config endpoint."""
    return await _post_automation_config(
        base_url, token, automation_id, automation_config, default_alias="Updated automation"
    )


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
            return response.status == 200


_LIVE_CONTEXT_INTERESTING_ATTRIBUTES = frozenset(
    {
        "temperature",
        "current_temperature",
        "temperature_unit",
        "brightness",
        "humidity",
        "unit_of_measurement",
        "device_class",
        "current_position",
        "percentage",
        "volume_level",
        "media_title",
        "media_artist",
        "media_album_name",
    }
)


async def get_live_context(
    ws_url: str,
    token: str,
    name: str | None = None,
    domain: str | list[str] | None = None,
    area: str | None = None,
) -> dict[str, Any]:
    """Fetch real-time state for Home Assistant entities, mirroring the GetLiveContext MCP tool.

    Retrieves current entity states from Home Assistant and returns them with
    their interesting attributes, optionally filtered by name, domain, and/or
    area.  The filters mirror those accepted by the ``GetLiveContext`` tool
    exposed by the HA MCP Server integration.

    :param ws_url: Home Assistant URL (http/https/ws/wss; the websocket path
        is added automatically).
    :param token: Long-lived access token.
    :param name: Case-insensitive substring filter applied to the entity's
        ``friendly_name`` and ``entity_id``.
    :param domain: Domain or list of domains to include (e.g. ``"light"`` or
        ``["light", "switch"]``).  Case-insensitive.
    :param area: Case-insensitive area name (or alias) filter.
    :returns: ``{"success": True, "entities": [...]}`` on success, or
        ``{"success": False, "error": "..."}`` when no entities match.
    """
    ws_url = normalize_ha_ws_url(ws_url)

    # Normalise domain filter to a set of lowercase strings (or None)
    domain_filter: set[str] | None = None
    if isinstance(domain, str):
        domain_filter = {domain.strip().lower()} if domain.strip() else None
    elif isinstance(domain, list):
        domain_filter = {d.strip().lower() for d in domain if d.strip()} or None

    async with HAWebSocketClient(ws_url, token) as ha:
        states = await ha.call("get_states") or []
        areas = await ha.call("config/area_registry/list") or []
        entities = await ha.call("config/entity_registry/list") or []
        expose_resp = await ha.call("homeassistant/expose_entity/list") or {}

    # ---- build set of exposed entity IDs ------------------------------------
    # expose_resp["exposed_entities"] maps entity_id → {assistant: bool, ...}.
    # An entity is considered exposed if at least one assistant has it set to True.
    raw_exposed: dict[str, Any] = expose_resp.get("exposed_entities", expose_resp) if isinstance(expose_resp, dict) else {}
    exposed_entity_ids: set[str] = {
        eid
        for eid, assistants in raw_exposed.items()
        if isinstance(assistants, dict) and any(v is True for v in assistants.values())
    }

    # ---- build lookup tables ------------------------------------------------
    area_name_by_id: dict[str, str] = {
        a["area_id"]: a.get("name", "") for a in areas if a.get("area_id")
    }
    # Map every area name and alias (lower-cased) → area_id for area filtering
    area_id_by_name: dict[str, str] = {}
    for a in areas:
        aid = a.get("area_id")
        if not aid:
            continue
        aname = a.get("name", "")
        if aname:
            area_id_by_name[aname.lower()] = aid
        for alias in a.get("aliases") or []:
            if alias:
                area_id_by_name[alias.lower()] = aid

    entity_reg: dict[str, dict[str, Any]] = {
        e["entity_id"]: e for e in entities if e.get("entity_id")
    }

    # ---- resolve the target area_id when an area filter is given ------------
    target_area_id: str | None = None
    if area is not None:
        target_area_id = area_id_by_name.get(area.strip().lower())
        if target_area_id is None:
            return {"success": False, "error": f"Area '{area}' does not exist"}

    # ---- iterate states and apply filters -----------------------------------
    result_entities: list[dict[str, Any]] = []

    for state in sorted(
        states,
        key=lambda s: (
            s.get("attributes", {}).get("friendly_name") or s.get("entity_id") or ""
        ),
    ):
        entity_id: str = state.get("entity_id", "")
        if not entity_id or "." not in entity_id:
            continue

        # -- only exposed entities --
        if entity_id not in exposed_entity_ids:
            continue

        entity_domain = entity_id.split(".")[0]

        # -- domain filter --
        if domain_filter is not None and entity_domain not in domain_filter:
            continue

        friendly_name: str = state.get("attributes", {}).get("friendly_name", "")

        # -- name filter (substring, case-insensitive) --
        if name is not None:
            needle = name.strip().lower()
            if needle not in friendly_name.lower() and needle not in entity_id.lower():
                continue

        # -- resolve entity area via entity registry --------
        e_entry = entity_reg.get(entity_id, {})
        entity_area_id: str | None = e_entry.get("area_id")

        # -- area filter --
        if target_area_id is not None and entity_area_id != target_area_id:
            continue

        # -- assemble entity info ---------------------------------------------
        attrs = {
            k: v
            for k, v in state.get("attributes", {}).items()
            if k in _LIVE_CONTEXT_INTERESTING_ATTRIBUTES
        }

        info: dict[str, Any] = {
            "entity_id": entity_id,
            "name": friendly_name or entity_id,
            "domain": entity_domain,
            "state": state.get("state"),
        }
        if entity_area_id:
            info["area"] = area_name_by_id.get(entity_area_id, entity_area_id)
        if attrs:
            info["attributes"] = attrs

        result_entities.append(info)

    if not result_entities:
        if domain_filter or name or area:
            return {"success": False, "error": "No entities matched the provided filter"}
        return {"success": False, "error": "No entities found"}

    return {"success": True, "entities": result_entities}
