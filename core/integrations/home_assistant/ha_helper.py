from typing import Any, Dict, List, Optional

from .ha_web_socket_client import HAWebSocketClient


async def fetch_devices_entities_with_location(
    ws_url: str,
    token: str,
) -> List[Dict[str, Any]]:
    async with HAWebSocketClient(ws_url, token) as ha:
        # Core registries
        areas = await ha.call("config/area_registry/list")
        devices = await ha.call("config/device_registry/list")
        entities = await ha.call("config/entity_registry/list")

        area_name_by_id = {a["area_id"]: a.get("name") for a in areas}

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

                ents.append(
                    {
                        "entity_id": e.get("entity_id"),
                        "unique_id": e.get("unique_id"),
                        "platform": e.get("platform"),
                        "area": entity_area_name,
                        # "disabled_by": e.get("disabled_by"),
                        # "hidden_by": e.get("hidden_by"),
                        "original_name": e.get("original_name"),
                        "name": e.get("name"),
                    }
                )

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
