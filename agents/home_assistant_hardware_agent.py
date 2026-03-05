"""
HomeAssistantHardwareAgent - Selects hardware needed for Home Assistant automations.
This agent only recommends hardware. It does not create automations.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from ..core.integrations.home_assistant.ha_helper import fetch_devices_entities_with_location

# from ..ha_helper import fetch_devices_entities_with_location

from ..core.actor import Message, MessageType
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)

HARDWARE_SELECTION_PROMPT = """You are a Home Assistant hardware selection specialist.

Task:
- Select the best available hardware for the user automation request.
- You MUST NOT create the automation. You ONLY recommend hardware.
- If no relevant hardware is found, return can_fulfill=false.

Input:
- user_request: natural language request
- device_discovery: Home Assistant connection and discovered devices
- device_discovery.devices: list of objects with this schema:
    {
        "device_id": string,
        "name": string,
        "manufacturer": string,
        "model": string,
        "area": string,
        "entities": [
            {
                "entity_id": string,
                "unique_id": string,
                "platform": string,
                "area": string,
                "original_name": string,
                "name": string
            }
        ]
    }

Rules:
- If device_discovery.connected is true, ground recommendations in discovered devices/entities.
- Prefer specific, minimal, high-confidence recommendations.
- Include optional coordinator recommendation only when it helps.
- If connected=true but no relevant hardware available, return cannot do it with current hardware.
- If can_fulfill=false because hardware is missing, result MUST explicitly list what is missing (device types/capabilities/protocol hints) needed to fulfill the request.
- For cannot-fulfill responses, start result with: "Missing hardware:" and provide a concise, concrete list.
- When possible, explain why currently discovered devices/entities are insufficient.
- If connected=false, you can still recommend best-practice hardware based on the request.
- If can_fulfill is true, hardware MUST contain at least one item.
- NEVER return can_fulfill=true with hardware=[].
- If unsure, set can_fulfill=false.
- Do not say "existing hardware is enough" unless you also list the specific hardware items in hardware[].

Validation before final answer:
1) If can_fulfill=true then len(hardware) >= 1.
2) Each hardware item must include hardware, why, protocol, required_domains.
3) If device_discovery.connected=true and no matching available hardware exists, set can_fulfill=false.
4) If possible, include required_entities with specific entity_id values from devices[].
5) If can_fulfill=false, result must include a "Missing hardware:" list with at least one concrete missing item.

Examples:
Valid:
{
    "can_fulfill": true,
    "result": "You can do this with a door contact sensor and a smart light.",
    "hardware": [
        {
            "hardware": "Door/Window contact sensor",
            "why": "Detects bedroom door open event.",
            "protocol": "Zigbee",
            "required_domains": ["binary_sensor"],
            "required_entities": ["binary_sensor.bedroom_door"]
        },
        {
            "hardware": "Smart bulb or smart wall switch",
            "why": "Turns kitchen light on.",
            "protocol": "Zigbee or Wi-Fi",
            "required_domains": ["light", "switch"],
            "required_entities": ["light.kitchen_ceiling", "switch.kitchen_lamp"]
        }
    ]
}

Invalid (DO NOT DO THIS):
{
    "can_fulfill": true,
    "result": "You can achieve this automation with existing hardware.",
    "hardware": []
}

Valid cannot-fulfill:
{
    "can_fulfill": false,
    "result": "Missing hardware:\n- Presence sensor in hallway (domain: binary_sensor) to detect occupancy.\n- Smart relay/switch for hallway lights (domain: switch or light).\nCurrent discovered devices do not expose these entities.",
    "hardware": []
}

Output strict JSON object only with keys:
{
    "can_fulfill": boolean,
    "result": string,
    "hardware": [
        {
            "hardware": string,
            "why": string,
            "protocol": string,
            "required_domains": [string],
            "required_entities": [string]
        }
    ]
}
"""


class HomeAssistantHardwareAgent(LLMAgent):
    """Recommend the best hardware for a requested Home Assistant automation."""

    def __init__(self, llm_provider: LLMProvider | None = None, **kwargs):
        kwargs.setdefault("name", "home-assistant-hardware")
        kwargs.setdefault("system_prompt", HARDWARE_SELECTION_PROMPT)
        super().__init__(llm_provider=llm_provider, **kwargs)
        self.ha_url = (os.getenv("HOME_ASSISTANT_URL") or os.getenv("HA_URL") or "").rstrip("/")
        self.ha_token = os.getenv("HOME_ASSISTANT_TOKEN") or os.getenv("HA_TOKEN") or ""
        self._device_cache: dict[str, Any] = {"timestamp": 0.0, "data": None}
        self._device_cache_ttl_seconds = 30.0

    async def chat(self, user_message: str) -> str:
        """Direct chat entry point used by CLI when addressing this agent."""
        available = await self._get_available_devices()
        result = await self._recommend_hardware(user_message, available)
        return result["result"]

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return

        text = self._extract_text(msg.payload)
        available = await self._get_available_devices()
        result = await self._recommend_hardware(text, available)
        if isinstance(result, dict):
            result.setdefault("task", text)

        self.metrics.tasks_completed += 1
        if msg.sender_id:
            await self.send(msg.sender_id, MessageType.RESULT, result)

    @staticmethod
    def _extract_text(payload: Any) -> str:
        if isinstance(payload, dict):
            if isinstance(payload.get("text"), str):
                return payload["text"]
            if isinstance(payload.get("task"), str):
                return payload["task"]
        return str(payload)

    @staticmethod
    def _derive_domains_from_devices(devices: list[dict[str, Any]]) -> set[str]:
        domains: set[str] = set()
        for device in devices:
            for entity in device.get("entities", []) or []:
                entity_id = str(entity.get("entity_id", ""))
                if "." in entity_id:
                    domain, _ = entity_id.split(".", 1)
                    domains.add(domain)
        return domains

    async def _get_available_devices(self) -> dict[str, Any]:
        now = time.time()
        if (
            self._device_cache.get("data") is not None
            and now - float(self._device_cache.get("timestamp", 0.0)) < self._device_cache_ttl_seconds
        ):
            return self._device_cache["data"]

        if not self.ha_url or not self.ha_token:
            data = {
                "connected": False,
                "domains": set(),
                "devices": [],
                "reason": "HOME_ASSISTANT_URL or HOME_ASSISTANT_TOKEN is not configured",
            }
            self._device_cache = {"timestamp": now, "data": data}
            return data

        try:
            devices = await fetch_devices_entities_with_location(self.ha_url, self.ha_token, include_states=True)
            if not isinstance(devices, list):
                devices = []

            domains = self._derive_domains_from_devices(devices)

            data = {
                "connected": True,
                "domains": domains,
                "devices": devices,
                "reason": "",
            }
            self._device_cache = {"timestamp": now, "data": data}
            return data

        except Exception as exc:
            data = {
                "connected": False,
                "domains": set(),
                "devices": [],
                "reason": f"Could not query Home Assistant devices: {exc}",
            }
            self._device_cache = {"timestamp": now, "data": data}
            return data

    def _format_result(
        self,
        request_text: str,
        available: dict[str, Any],
        selected_hardware: list[dict[str, Any]],
        can_fulfill: bool,
        fallback_text: str = "",
    ) -> dict[str, Any]:
        connected = bool(available.get("connected"))

        if not selected_hardware or not can_fulfill:
            cannot = fallback_text
            if not cannot:
                if connected:
                    cannot = (
                        "I found Home Assistant devices, but none relevant to this automation request are currently available. "
                        "I cannot do it with the current hardware."
                    )
                else:
                    cannot = (
                        "I cannot determine relevant Home Assistant hardware from that request. "
                        "Please describe a trigger and an action, for example: "
                        "'when a door opens, turn on a light'."
                    )
            return {
                "can_fulfill": False,
                "task": request_text,
                "request": request_text,
                "hardware": [],
                "result": cannot,
                "device_discovery": {
                    "connected": connected,
                    "reason": available.get("reason", ""),
                },
            }

        lines: list[str] = ["Best hardware for this automation:"]
        for rec in selected_hardware:
            line = f"- {rec.get('hardware', 'Unknown')} ({rec.get('protocol', 'N/A')}) — {rec.get('why', '')}"
            if connected:
                required_entities = rec.get("required_entities") or []
                if isinstance(required_entities, list) and required_entities:
                    shown = [str(entity_id) for entity_id in required_entities[:3]]
                    line += f" Available: {', '.join(shown)}"
            lines.append(line)

        if connected:
            lines.append("Hardware selection is based on currently discovered Home Assistant entities.")
        else:
            reason = available.get("reason", "Device discovery not available")
            lines.append(f"Device discovery unavailable: {reason}.")

        lines.append("This agent only selects hardware and does not create the automation.")
        return {
            "can_fulfill": True,
            "task": request_text,
            "request": request_text,
            "hardware": selected_hardware,
            "result": "\n".join(lines),
            "device_discovery": {
                "connected": connected,
                "reason": available.get("reason", ""),
            },
        }

    async def _recommend_hardware_with_llm(self, request_text: str, available: dict[str, Any]) -> dict[str, Any] | None:
        if self.llm is None:
            return None

        devices = available.get("devices", []) or []
        payload = {
            "user_request": request_text,
            "device_discovery": {
                "connected": bool(available.get("connected")),
                "reason": available.get("reason", ""),
                "domains": sorted(list(available.get("domains", set()) or set())),
                "devices": devices,
            },
        }

        user_input = {"role": "user", "content": json.dumps(payload)}

        response, _ = await self.llm.complete(messages=[user_input], system=self.system_prompt)
        cleaned = (response or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*", "", cleaned).strip()
            cleaned = re.sub(r"```$", "", cleaned).strip()

        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return None

        selected = data.get("hardware") or []
        if not isinstance(selected, list):
            selected = []
        can_fulfill = bool(data.get("can_fulfill"))
        fallback_text = str(data.get("result", "")).strip()

        if can_fulfill and not selected:
            correction = {
                "role": "user",
                "content": (
                    "Your previous JSON is invalid for this task because can_fulfill=true but hardware is empty. "
                    "Return corrected JSON only. Either provide at least one concrete hardware item, "
                    "or set can_fulfill=false."
                ),
            }
            retry = await self.llm.complete(
                messages=[user_input, {"role": "assistant", "content": response}, correction],
                system=self.system_prompt,
            )
            retry_cleaned = (retry or "").strip()
            if retry_cleaned.startswith("```"):
                retry_cleaned = re.sub(r"^```[a-zA-Z]*", "", retry_cleaned).strip()
                retry_cleaned = re.sub(r"```$", "", retry_cleaned).strip()

            retry_data = json.loads(retry_cleaned)
            if isinstance(retry_data, dict):
                data = retry_data
                selected = data.get("hardware") or []
                if not isinstance(selected, list):
                    selected = []
                can_fulfill = bool(data.get("can_fulfill"))
                fallback_text = str(data.get("result", "")).strip()

        if can_fulfill and not selected:
            can_fulfill = False
            fallback_text = (
                "I cannot return a valid hardware recommendation because the LLM response was inconsistent. "
                "Please retry the request."
            )

        return self._format_result(request_text, available, selected, can_fulfill, fallback_text)

    async def _recommend_hardware(self, request_text: str, available: dict[str, Any]) -> dict[str, Any]:
        llm_result = await self._recommend_hardware_with_llm(request_text, available)
        # llm_result = None  # Disable LLM for now to test fallback behavior  
        if llm_result is not None:
            return llm_result

        if self.llm is None:
            fallback_text = (
                "I cannot select hardware because no LLM provider is configured for this agent. "
                "Configure an LLM provider and retry."
            )
        else:
            fallback_text = (
                "I cannot return a valid hardware recommendation because the LLM response could not be parsed. "
                "Please retry the request."
            )

        return self._format_result(
            request_text=request_text,
            available=available,
            selected_hardware=[],
            can_fulfill=False,
            fallback_text=fallback_text,
        )
