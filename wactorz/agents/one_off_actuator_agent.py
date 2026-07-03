from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from typing import Any, ClassVar

from wactorz.config import CONFIG

from ..core.actor import Actor, Message, MessageType
from ..core.integrations.home_assistant.ha_helper import (
    fetch_devices_entities_with_location,
    normalize_ha_ws_url,
)
from ..core.integrations.home_assistant.ha_web_socket_client import HAWebSocketClient
from .home_assistant_actuator_agent import ActuatorAction
from .llm_agent import LLMProvider, _accumulate_global_cost

logger = logging.getLogger(__name__)

_COLOR_RGB = {
    "blue": [0, 0, 255],
    "pink": [255, 105, 180],
    "red": [255, 0, 0],
    "green": [0, 255, 0],
    "purple": [128, 0, 128],
    "white": [255, 255, 255],
}
_COLOR_SERVICE_KEYS = {
    "rgb_color",
    "hs_color",
    "xy_color",
    "rgbw_color",
    "rgbww_color",
    "color_name",
}
_COLOR_MODES = {"hs", "rgb", "rgbw", "rgbww", "xy"}

_RESOLVER_PROMPT = """You are a Home Assistant service-call resolver.

Your task:
- Convert the user's natural-language device control request into one or more Home Assistant service calls.
- Use only entities that exist in the provided Home Assistant device discovery payload.
- Return strict JSON only: an array of action objects.

Action schema:
[
  {
    "domain": "light",
    "service": "turn_on",
    "entity_id": "light.living_room_lamp",
    "service_data": {"brightness_pct": 50}
  }
]

Rules:
- Return an array, never an object.
- Use the most specific matching entity_id available.
- If the request is ambiguous or no device matches, return [].
- For multiple commands in one request, return multiple actions.
- Only include service_data keys that are needed.
- Common examples:
  - turn on/off light or switch -> light.turn_on / light.turn_off or switch.turn_on / switch.turn_off
  - set a light color -> light.turn_on with service_data containing rgb_color
  - set heating/thermostat temperature -> climate.set_temperature with {"temperature": number}
  - lock/unlock door -> lock.lock / lock.unlock
  - open/close cover/blinds -> cover.open_cover / cover.close_cover
  - brightness percent -> use {"brightness_pct": number}
- Color requests:
  - For "blue", use {"rgb_color": [0, 0, 255]}.
  - For "pink", use {"rgb_color": [255, 105, 180]}.
  - For "red", use {"rgb_color": [255, 0, 0]}; "green" -> [0, 255, 0]; "purple" -> [128, 0, 128]; "white" -> [255, 255, 255].
  - If the user says just "my light" / "the light" and a color is requested, prefer a color-capable light entity over a color-temperature-only light. Color-capable entities usually have state attributes such as supported_color_modes containing hs, rgb, rgbw, rgbww, or xy, or current color_mode hs/rgb/xy.
  - Do not send color service_data to a light that only supports color_temp.
- Do not invent entity IDs.
- Do not return markdown or explanation.
"""


class OneOffActuatorAgent(Actor):
    """Ephemeral actor that resolves and executes one-shot HA service calls."""

    DESCRIPTION = "Ephemeral Home Assistant actuator for one-shot natural-language device control"
    CAPABILITIES: ClassVar[list[str]] = [
        "home_automation",
        "ha_actuation",
        "device_control",
        "one_shot_actuation",
    ]

    def __init__(
        self,
        request: str,
        llm_provider: LLMProvider | None,
        task_id: str,
        reply_to_id: str,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("name", f"one-off-actuator-{task_id[-8:]}")
        super().__init__(**kwargs)
        self.request = request
        self.llm = llm_provider
        self.task_id = task_id
        self.reply_to_id = reply_to_id
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self._last_period_cost_usd = 0.0

    def _current_task_description(self) -> str:
        return self.request[:60] if self.request else "one-shot actuation"

    async def on_start(self) -> None:
        await self.publish_manifest(
            description=self.DESCRIPTION,
            capabilities=self.CAPABILITIES,
            input_schema={
                "request": "str — natural-language Home Assistant device control request",
                "task_id": "str — correlation id for the parent future",
                "reply_to_id": "str — actor id that should receive the RESULT message",
            },
            output_schema={
                "result": "str — human-readable summary of executed Home Assistant service calls",
                "_task_id": "str — correlation id echoed back to the parent actor",
            },
        )
        asyncio.create_task(self._run())

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            return

    async def _run(self) -> None:
        try:
            result = await self._execute_request()
            self.metrics.tasks_completed += 1
            await self._mqtt_publish(
                f"agents/{self.actor_id}/metrics",
                self._build_metrics(),
            )
            await self._send_result(result)
        except Exception as exc:
            self.metrics.tasks_failed += 1
            await self._mqtt_publish(
                f"agents/{self.actor_id}/metrics",
                self._build_metrics(),
            )
            logger.error("[%s] One-shot actuation failed: %s", self.name, exc, exc_info=True)
            await self._send_result(f"Actuation failed: {exc}")
        finally:
            asyncio.create_task(self._deferred_stop())

    async def _execute_request(self) -> str:
        if not CONFIG.ha_url or not CONFIG.ha_token:
            return (
                "Home Assistant is not configured. Set `HA_URL` and `HA_TOKEN` in your .env file."
            )
        if self.llm is None:
            return "Actuation failed: no LLM provider is available."

        devices = await fetch_devices_entities_with_location(
            CONFIG.ha_url,
            CONFIG.ha_token,
            include_states=True,
        )
        actions = await self._resolve_actions(devices)
        if not actions:
            return "I couldn't identify a matching device for that request."

        return await self._execute_actions(actions)

    async def _resolve_actions(self, devices: list[dict[str, Any]]) -> list[ActuatorAction]:
        prompt_input = {
            "user_request": self.request,
            "devices": devices,
        }
        raw, usage = await asyncio.wait_for(
            self.llm.complete(
                messages=[{"role": "user", "content": json.dumps(prompt_input)}],
                system=_RESOLVER_PROMPT,
                max_tokens=1200,
            ),
            timeout=120.0,
        )
        self._accumulate_usage(usage)
        parsed = self._parse_actions_json(raw)
        actions = [ActuatorAction.from_dict(item) for item in parsed]
        return self._repair_color_actions(actions, devices)

    def _parse_actions_json(self, raw: str) -> list[dict[str, Any]]:
        cleaned = (raw or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned.removeprefix("json")
            cleaned = cleaned.strip()
        data = json.loads(cleaned)
        if not isinstance(data, list):
            raise json.JSONDecodeError("Expected a JSON array", cleaned, 0)
        return [item for item in data if isinstance(item, dict)]

    def _requested_rgb(self) -> list[int] | None:
        request = self.request.lower()
        for color, rgb in _COLOR_RGB.items():
            if color in request:
                return list(rgb)
        return None

    def _requests_max_brightness(self) -> bool:
        request = self.request.lower()
        return any(
            phrase in request
            for phrase in (
                "max brightness",
                "maximum brightness",
                "full brightness",
                "brightest",
                "100% brightness",
                "100 percent brightness",
            )
        )

    def _repair_color_actions(
        self,
        actions: list[ActuatorAction],
        devices: list[dict[str, Any]],
    ) -> list[ActuatorAction]:
        rgb = self._requested_rgb()
        if rgb is None:
            return actions

        color_entity = self._find_color_light(devices)
        repaired: list[ActuatorAction] = []
        has_light_action = False
        for action in actions:
            if action.domain == "light" and action.service == "turn_on":
                has_light_action = True
                if color_entity and not self._entity_id_supports_color(action.entity_id, devices):
                    action.entity_id = color_entity
                action.service_data = dict(action.service_data or {})
                if not any(key in action.service_data for key in _COLOR_SERVICE_KEYS):
                    action.service_data["rgb_color"] = rgb
                if self._requests_max_brightness() and "brightness_pct" not in action.service_data:
                    action.service_data["brightness_pct"] = 100
            repaired.append(action)

        if not has_light_action and color_entity:
            service_data: dict[str, Any] = {"rgb_color": rgb}
            if self._requests_max_brightness():
                service_data["brightness_pct"] = 100
            repaired.append(
                ActuatorAction(
                    domain="light",
                    service="turn_on",
                    entity_id=color_entity,
                    service_data=service_data,
                )
            )
        return repaired

    def _find_color_light(self, devices: list[dict[str, Any]]) -> str | None:
        lights = [
            entity for entity in self._iter_entities(devices) if self._entity_supports_color(entity)
        ]
        if not lights:
            return None

        request = self.request.lower()
        ranked: list[tuple[int, str]] = []
        for entity in lights:
            entity_id = str(entity.get("entity_id") or "")
            attrs = self._entity_attrs(entity)
            haystack = " ".join(
                str(value).lower()
                for value in (
                    entity_id,
                    entity.get("name"),
                    entity.get("friendly_name"),
                    attrs.get("friendly_name"),
                    entity.get("area"),
                    entity.get("location"),
                )
                if value
            )
            score = 0
            for token in request.replace("_", " ").replace("-", " ").split():
                token = token.strip(".,!?()[]{}'\"")
                if len(token) > 2 and token in haystack:
                    score += 1
            ranked.append((score, entity_id))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][1] if ranked else None

    def _entity_id_supports_color(self, entity_id: str, devices: list[dict[str, Any]]) -> bool:
        for entity in self._iter_entities(devices):
            if str(entity.get("entity_id") or "") == entity_id:
                return self._entity_supports_color(entity)
        return False

    def _iter_entities(self, node: Any):
        if isinstance(node, dict):
            entity_id = str(node.get("entity_id") or "")
            if entity_id.startswith("light."):
                yield node
            for value in node.values():
                yield from self._iter_entities(value)
        elif isinstance(node, list):
            for item in node:
                yield from self._iter_entities(item)

    def _entity_supports_color(self, entity: dict[str, Any]) -> bool:
        attrs = self._entity_attrs(entity)
        modes = attrs.get("supported_color_modes") or []
        if isinstance(modes, str):
            modes = [modes]
        mode_set = {str(mode).lower() for mode in modes}
        color_mode = str(attrs.get("color_mode") or "").lower()
        return bool(mode_set & _COLOR_MODES) or color_mode in _COLOR_MODES

    def _entity_attrs(self, entity: dict[str, Any]) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        state = entity.get("state")
        if isinstance(state, dict) and isinstance(state.get("attributes"), dict):
            attrs.update(state["attributes"])
        for key in ("attributes", "state_attributes"):
            if isinstance(entity.get(key), dict):
                attrs.update(entity[key])
        return attrs

    async def _execute_actions(self, actions: list[ActuatorAction]) -> str:
        ws_url = normalize_ha_ws_url(CONFIG.ha_url)
        successes: list[str] = []
        failures: list[str] = []

        async with HAWebSocketClient(ws_url, CONFIG.ha_token) as ha:
            for action in actions:
                try:
                    await ha.call_service(
                        action.domain,
                        action.service,
                        action.entity_id,
                        **(action.service_data or {}),
                    )
                    successes.append(self._format_action(action))
                except Exception as exc:
                    failures.append(f"{self._format_action(action)} ({exc})")

        if successes and not failures:
            return f"Done: {', '.join(successes)}."
        if failures and not successes:
            return "Nothing was executed successfully: " + "; ".join(failures)
        return (
            "Partial success. Completed: "
            + ", ".join(successes)
            + ". Failed: "
            + "; ".join(failures)
        )

    def _format_action(self, action: ActuatorAction) -> str:
        return f"{action.domain}.{action.service} -> {action.entity_id}"

    async def _send_result(self, result: str) -> None:
        if not self.reply_to_id:
            return
        await self.send(
            self.reply_to_id,
            MessageType.RESULT,
            {"result": result, "_task_id": self.task_id},
        )

    def _accumulate_usage(self, usage: dict[str, Any]) -> None:
        if not isinstance(usage, dict):
            return
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd += usage.get("cost_usd", 0.0)
        delta = self.total_cost_usd - self._last_period_cost_usd
        if delta > 0:
            _accumulate_global_cost(delta)
            self._last_period_cost_usd = self.total_cost_usd

    async def _deferred_stop(self) -> None:
        await asyncio.sleep(2.0)
        await self._log("Self-terminating.")
        if self._registry:
            await self._registry.unregister(self.actor_id)
        await self.stop()
        self._delete_persistence_dir()

    async def _log(self, msg: str) -> None:
        logger.info("[%s] %s", self.name, msg)
        await self._mqtt_publish(
            f"agents/{self.actor_id}/logs",
            {"type": "log", "message": msg, "timestamp": time.time()},
        )

    def _delete_persistence_dir(self) -> None:
        try:
            shutil.rmtree(self._persistence_dir, ignore_errors=False)
        except FileNotFoundError:
            return
        except Exception as exc:
            logger.warning(
                "[%s] Failed to delete persistence dir %s: %s",
                self.name,
                self._persistence_dir,
                exc,
            )

    def _build_metrics(self) -> dict[str, Any]:
        metrics = super()._build_metrics()
        metrics["input_tokens"] = self.total_input_tokens
        metrics["output_tokens"] = self.total_output_tokens
        metrics["cost_usd"] = round(self.total_cost_usd, 6)
        return metrics
