"""
HomeAssistantAgent - Unified Home Assistant agent.

Handles all HA operations in a single agent:
  - recommend_hardware    : advise which devices/entities are needed
  - create_automation     : build and insert a new automation via REST (temporarily disabled — routes to recommend_hardware)
  - delete_automation     : remove an existing automation
  - edit_automation       : update an existing automation
  - list_automations      : enumerate all automations
  - list_areas            : enumerate Home Assistant areas
  - list_devices          : enumerate Home Assistant devices
  - list_entities         : enumerate Home Assistant entities
  - get_entities_state    : fetch current states for explicit entity IDs


Intent is classified with a cheap single-word LLM call, then the
appropriate code path runs.  Complex operations (create, edit) use up
to two additional LLM calls internally; simpler ones (list, delete) use
one.  All HA communication goes through ha_helper.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from wactorz.config import CONFIG

from ..core.actor import Message, MessageType
from ..core.integrations.home_assistant.ha_helper import (
    create_automation_via_rest,
    delete_automation,
    get_areas,
    get_automations,
    get_devices,
    get_entities,
    get_states,
    get_simplified_ha_data,
    update_automation,
)
from .prompts.home_assistant_prompts import (
    AUTOMATION_CREATION_PROMPT,
    HA_ACTION_CLASSIFICATION_PROMPT,
    HA_OTHER_PROMPT,
    HA_OTHER_TOOL,
    HARDWARE_RECOMMENDATION_PROMPT,
    HARDWARE_SELECTION_PROMPT,
    HA_DELETE_CONFIRM_PROMPT,
    HA_IDENTIFY_AUTOMATION_PROMPT,
    HA_EDIT_AUTOMATION_PROMPT
)
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)


class AutomationEditError(Exception):
    """Internal error used to map edit helper failures to the public edit response."""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class HomeAssistantAgent(LLMAgent):
    """Unified Home Assistant agent: hardware recommendations and automation CRUD."""
    DESCRIPTION   = "Controls Home Assistant: automations, devices, areas, entities"
    CAPABILITIES  = ["home_automation", "ha_automations", "ha_devices", "ha_entities"]
    INPUT_SCHEMA  = {
        "text": "str — natural language command or query, e.g. 'turn on living room lights', "
                "'list all automations', 'create automation that turns off lights at 11pm'"
    }
    OUTPUT_SCHEMA = {
        "result": "str — human-readable confirmation or list of results",
        "data":   "list|dict|null — structured HA API response when applicable"
    }

    def __init__(self, llm_provider: LLMProvider | None = None, **kwargs) -> None:
        kwargs.setdefault("name", "home-assistant-agent")
        kwargs.setdefault("system_prompt", AUTOMATION_CREATION_PROMPT)
        super().__init__(llm_provider=llm_provider, **kwargs)
        self.ha_url = (CONFIG.ha_url).rstrip("/")
        self.ha_token = (CONFIG.ha_token).strip()
        self._device_cache: dict[str, Any] = {"timestamp": 0.0, "data": None}
        self._device_cache_ttl = 30.0
        self._automation_cache: dict[str, Any] = {"timestamp": 0.0, "data": None}
        self._automation_cache_ttl = 30.0
        self._other_tool_max_rounds = 3

    # ── Cost tracking helper ─────────────────────────────────────────────────

    def _accumulate_usage(self, usage: dict) -> None:
        """Add token counts and cost from one llm.complete() call to running totals."""
        if not isinstance(usage, dict):
            return
        self.total_input_tokens  += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd      += usage.get("cost_usd", 0.0)
        self._persist_cost()


    # ── Public entry points ──────────────────────────────────────────────────

    async def chat(self, user_message: str) -> str:
        """Direct entry point used by CLI when addressing this agent."""
        ts_user = time.time()
        self._conversation_history.append({"role": "user", "content": user_message, "ts": ts_user})
        result = await self._process(user_message)
        response = str(result.get("result", ""))
        ts_reply = time.time()
        self._conversation_history.append({"role": "assistant", "content": response, "ts": ts_reply})
        await self._maybe_summarize()
        self.persist("conversation_history", self._conversation_history)
        self._log_chat_turn(user_message, response, ts_user=ts_user, ts_reply=ts_reply)
        return response


    async def chat_stream(self, user_message: str):
        """
        Override LLMAgent streaming path so direct @home-assistant-agent calls
        still use Home Assistant intent routing instead of generic LLM chat.
        """
        response = await self.chat(user_message)
        yield response
        yield {}


    async def handle_message(self, msg: Message) -> None:
        if msg.type != MessageType.TASK:
            return

        text, entities, hardware = self._extract_payload(msg.payload)

        if entities or hardware:
            # Pre-selected entities/hardware provided (e.g. direct API call) — skip
            # classification and go straight to automation creation.
            result = await self._create_automation(text, entities, hardware)
        else:
            result = await self._process(text)

        if isinstance(result, dict):
            result.setdefault("task", self._extract_task_id(msg.payload, text))
            # Echo _task_id so planner futures resolve correctly
            if isinstance(msg.payload, dict) and msg.payload.get("_task_id"):
                result["_task_id"] = msg.payload["_task_id"]

        self.metrics.tasks_completed += 1
        if msg.sender_id:
            await self.send(msg.sender_id, MessageType.RESULT, result)


    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def _process(self, text: str) -> dict[str, Any]:
        """Classify intent then route to the appropriate handler."""
        action = await self._classify_action(text)
        logger.info("[%s] Classified action: %s", self.name, action)

        if action == "list_areas":
            return await self._list_areas()

        if action == "list_devices":
            return await self._list_devices()

        if action == "list_entities":
            return await self._list_entities()

        if action == "get_entities_state":
            return await self._handle_entities_state_request(text)

        if action == "list_automations":
            automations = await self._get_automations_brief()
            return self._list_automations(automations)

        if action == "delete_automation":
            automations = await self._get_automations_brief()
            return await self._delete_automation(text, automations)

        if action == "edit_automation":
            automations = await self._get_automations_brief()
            devices = await self._get_devices()
            logger.info("[%s] Got devices from Home Assistant", self.name)
            return await self._edit_automation(text, automations, devices)

        if action == "recommend_hardware":
            devices = await self._get_devices()
            logger.info("[%s] Got devices from Home Assistant", self.name)
            return await self._recommend_hardware(text, devices)

        if action == "create_automation":
            # Create flow: hardware selection then automation generation.
            # NOTE: the create_automation flow is temporarily disabled in _process.
            # instead, hardware recommendation is used.
            devices = await self._get_devices()
            logger.info("[%s] Got devices from Home Assistant", self.name)
            # hardware_result = await self._select_hardware(text, devices)
            # if not hardware_result.get("can_fulfill"):
            #     return hardware_result

            # entities = self._extract_entity_ids_from_hardware(hardware_result)
            # return await self._create_automation(text, entities, hardware_result.get("hardware", []))
            return await self._recommend_hardware(text, devices)

        if action == "other":
            return await self._handle_other_request(text)

        return self._unsupported_action_response(text)


    # ── Intent classification ────────────────────────────────────────────────

    async def _classify_action(self, text: str) -> str:
        """Return one action string via a cheap single-word LLM call."""
        valid = {
            "recommend_hardware",
            "create_automation",
            "delete_automation",
            "edit_automation",
            "list_automations",
            "list_areas",
            "list_devices",
            "list_entities",
            "other",
            "unknown",
        }

        heuristic = self._classify_action_heuristic(text)
        if heuristic == "get_entities_state":
            return heuristic

        if self.llm is None:
            logger.warning("[%s] No LLM provider configured; skipping action classification LLM call.", self.name)
            return heuristic

        try:
            response, usage = await self.llm.complete(
                messages=[{"role": "user", "content": text}],
                system=HA_ACTION_CLASSIFICATION_PROMPT,
                max_completion_tokens=20,
            )
            self._accumulate_usage(usage)
            word = (response or "").strip().lower().split()[0] if (response or "").strip() else ""
            if word in valid:
                return word
        except Exception as exc:
            logger.warning("[%s] Action classification LLM call failed: %s", self.name, exc)

        return heuristic

    @staticmethod
    def _classify_action_heuristic(text: str) -> str:
        lower = text.lower()
        if any(w in lower for w in ("list areas", "show areas", "show me areas", "what areas")):
            return "list_areas"
        if any(w in lower for w in ("list devices", "show devices", "show me devices", "what devices")):
            return "list_devices"
        if any(w in lower for w in ("list entities", "show entities", "show me entities", "what entities")):
            return "list_entities"
        if "get_entities_state" in lower:
            return "get_entities_state"
        if any(w in lower for w in ("list automations", "show automations", "show all automations", "what automations", "what are my automations")):
            return "list_automations"
        if any(w in lower for w in ("delete automation", "remove automation", "disable automation")):
            return "delete_automation"
        if any(w in lower for w in ("edit automation", "update automation", "change automation", "modify automation", "rename automation")):
            return "edit_automation"
        if (
            "automation" in lower
            and any(w in lower for w in ("create", "add", "new", "build", "make", "set up"))
        ):
            return "create_automation"
        if any(w in lower for w in ("hardware", "what device", "what sensor", "what do i need", "compatible with")):
            return "recommend_hardware"
        if any(w in lower for w in ("create", "add automation", "new automation", "build automation", "make automation")):
            return "create_automation"
        ha_context_terms = (
            "home assistant", "hass", "entity", "entities", "device", "devices",
            "sensor", "sensors", "light", "lights", "switch", "thermostat",
            "thermometer", "thermometers", "temperature", "humidity", "garage", "kitchen", "bedroom",
            "living room", "hallway", "bathroom", "room", "rooms",
        )
        if re.search(r"\bha\b", lower) or any(term in lower for term in ha_context_terms):
            return "other"
        return "unknown"


    @staticmethod
    def _unsupported_action_response(text: str) -> dict[str, Any]:
        return {
            "task": text,
            "result": (
                "I can help with Home Assistant hardware recommendations and automations: "
                "create, edit, delete, list automations, list areas, list devices, and list entities."
            ),
        }


    async def _handle_entities_state_request(self, text: str) -> dict[str, Any]:
        entity_ids = self._extract_entity_ids(text)
        if not entity_ids:
            return {
                "task": text,
                "result": "Please include one or more explicit Home Assistant entity IDs, like sensor.kitchen_temperature.",
                "error": "explicit_entity_id_required",
            }
        if not self.ha_url or not self.ha_token:
            return {
                "task": text,
                "result": "HA_URL or HA_TOKEN not configured.",
                "error": "HA_URL or HA_TOKEN not configured.",
            }

        try:
            states = await get_states(self.ha_url, self.ha_token)
        except Exception as exc:
            return {"task": text, "result": f"Home Assistant state query failed: {exc}", "error": str(exc)}

        states_by_id = {s.get("entity_id"): s for s in states or [] if isinstance(s, dict)}
        found = {eid: states_by_id[eid] for eid in entity_ids if eid in states_by_id}
        missing = [eid for eid in entity_ids if eid not in found]

        for entity_id, state_obj in found.items():
            # Publish using the original topic format (entity_id as topic) so
            # existing tests and subscribers are not broken, but include the
            # full state object and entity_id in the payload so that dynamic
            # agents filtering on payload.get('entity_id') work correctly.
            # homeassistant/state_changes/# wildcards match this topic since
            # entity_id is a subtopic path (e.g. sensor.living_room_temp).
            await self._mqtt_publish(
                f"homeassistant/state_changes/{entity_id}",
                {
                    "event_type": "state_changed",
                    "entity_id": entity_id,
                    "new_state": state_obj,
                    "old_state": None,
                },
            )

        parts = [f"{entity_id}: {state.get('state', 'unknown')}" for entity_id, state in found.items()]
        if missing:
            parts.append("Missing: " + ", ".join(missing))

        return {
            "task": text,
            "result": "; ".join(parts) if parts else "No requested entity states were found.",
            "data": {"states": found, "missing": missing},
        }


    async def _handle_other_request(self, text: str) -> dict[str, Any]:
        if not self.ha_url or not self.ha_token:
            return {
                "task": text,
                "result": "HA_URL or HA_TOKEN not configured.",
                "error": "HA_URL or HA_TOKEN not configured.",
            }
        if self.llm is None:
            return {
                "task": text,
                "result": "No LLM provider configured.",
                "error": "No LLM provider configured.",
            }

        messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
        tool_cache: dict[str, str] = {}
        tools = [HA_OTHER_TOOL]

        for _round in range(self._other_tool_max_rounds):
            try:
                completion = await self.llm.complete_with_tools(
                    messages=messages,
                    tools=tools,
                    system=HA_OTHER_PROMPT,
                    max_tokens=1200,
                )
            except Exception as exc:
                logger.warning("[%s] Other HA tool loop failed: %s", self.name, exc)
                return {
                    "task": text,
                    "result": f"Home Assistant tool request failed: {exc}",
                    "error": str(exc),
                }

            self._accumulate_usage(getattr(completion, "usage", {}))
            tool_calls = list(getattr(completion, "tool_calls", []) or [])
            if not tool_calls:
                content = str(getattr(completion, "content", "") or "").strip()
                return {"task": text, "result": content or "I could not answer that Home Assistant request."}

            assistant_message = getattr(completion, "assistant_message", None)
            if assistant_message:
                messages.append(assistant_message)

            for call in tool_calls:
                tool_name = getattr(call, "name", "")
                tool_call_id = getattr(call, "id", "") or tool_name
                if tool_name != "get_simplified_ha_data":
                    result_text = f"Unsupported tool: {tool_name}"
                    is_error = True
                else:
                    is_error = False
                    if tool_name not in tool_cache:
                        try:
                            data = await get_simplified_ha_data(self.ha_url, self.ha_token)
                            tool_cache[tool_name] = json.dumps(data, default=str)
                        except Exception as exc:
                            tool_cache[tool_name] = f"Home Assistant data fetch failed: {exc}"
                            is_error = True
                    result_text = tool_cache[tool_name]
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": result_text,
                        "is_error": is_error,
                    }
                )

        return {
            "task": text,
            "result": (
                "I could not complete that Home Assistant request within "
                f"{self._other_tool_max_rounds} tool rounds."
            ),
            "error": "tool_round_limit",
        }


    # ── Device discovery ─────────────────────────────────────────────────────

    async def _get_devices(self) -> dict[str, Any]:
        now = time.time()
        cached = self._device_cache.get("data")
        if cached is not None and now - float(self._device_cache.get("timestamp", 0.0)) < self._device_cache_ttl:
            return cached

        if not self.ha_url or not self.ha_token:
            data: dict[str, Any] = {
                "connected": False,
                "data": {},
                "reason": "HOME_ASSISTANT_URL or HOME_ASSISTANT_TOKEN is not configured",
            }
            self._device_cache = {"timestamp": now, "data": data}
            return data

        try:
            ha_data = await get_simplified_ha_data(self.ha_url, self.ha_token)
            if not isinstance(ha_data, dict):
                logger.warning("[%s] get_simplified_ha_data returned unexpected type %s", self.name, type(ha_data))
                ha_data = {}
            data = {"connected": True, "data": ha_data, "reason": ""}
            self._device_cache = {"timestamp": now, "data": data}
            return data
        except Exception as exc:
            data = {
                "connected": False,
                "data": {},
                "reason": f"Could not query Home Assistant devices: {exc}",
            }
            self._device_cache = {"timestamp": now, "data": data}
            return data


    async def _fetch_registry_items(self, fetcher: Any) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch HA registry data with common config and error handling."""
        if not self.ha_url or not self.ha_token:
            return [], "HA_URL or HA_TOKEN not configured."
        try:
            items = await fetcher(self.ha_url, self.ha_token)
            if not isinstance(items, list):
                items = []
            return items, None
        except Exception as exc:
            logger.warning("[%s] Could not fetch Home Assistant registry data: %s", self.name, exc)
            return [], f"Could not fetch data from Home Assistant: {exc}"


    async def _list_areas(self) -> dict[str, Any]:
        areas, error = await self._fetch_registry_items(get_areas)
        if error:
            return {"result": error, "areas": []}
        if not areas:
            return {"result": "No areas found in Home Assistant.", "areas": []}

        area_rows = [
            {
                "area_id": str(a.get("area_id", "")),
                "name": str(a.get("name") or "(unnamed)"),
            }
            for a in areas
            if isinstance(a, dict)
        ]
        lines = [f"Found {len(area_rows)} area(s) in Home Assistant:"]
        for idx, row in enumerate(area_rows, 1):
            lines.append(f"{idx}. {row['name']} ({row['area_id']})")
        return {"result": "\n".join(lines), "areas": area_rows}


    async def _list_devices(self) -> dict[str, Any]:
        devices, error = await self._fetch_registry_items(get_devices)
        if error:
            return {"result": error, "devices": []}
        if not devices:
            return {"result": "No devices found in Home Assistant.", "devices": []}

        device_rows = [
            {
                "device_id": str(d.get("id", "")),
                "name": str(d.get("name_by_user") or d.get("name") or "(unnamed)"),
                "manufacturer": str(d.get("manufacturer") or ""),
                "model": str(d.get("model") or ""),
            }
            for d in devices
            if isinstance(d, dict)
        ]
        lines = [f"Found {len(device_rows)} device(s) in Home Assistant:"]
        for idx, row in enumerate(device_rows, 1):
            details = " ".join(p for p in (row["manufacturer"], row["model"]) if p).strip()
            if details:
                lines.append(f"{idx}. {row['name']} ({details})")
            else:
                lines.append(f"{idx}. {row['name']}")
        return {"result": "\n".join(lines), "devices": device_rows}


    async def _list_entities(self) -> dict[str, Any]:
        entities, error = await self._fetch_registry_items(get_entities)
        if error:
            return {"result": error, "entities": []}
        if not entities:
            return {"result": "No entities found in Home Assistant.", "entities": []}

        entity_rows = [
            {
                "entity_id": str(e.get("entity_id", "")),
                "name": str(e.get("name") or e.get("original_name") or "(unnamed)"),
                "platform": str(e.get("platform") or ""),
            }
            for e in entities
            if isinstance(e, dict)
        ]
        lines = [f"Found {len(entity_rows)} entities in Home Assistant:"]
        for idx, row in enumerate(entity_rows, 1):
            if row["platform"]:
                lines.append(f"{idx}. {row['entity_id']} ({row['platform']})")
            else:
                lines.append(f"{idx}. {row['entity_id']}")
        return {"result": "\n".join(lines), "entities": entity_rows}


    # ── Hardware selection ────────────────────────────────────────────────────
    # NOTE: _select_hardware, _format_hardware_result, and _extract_entity_ids_from_hardware
    # are currently unused — the create_automation flow is temporarily disabled in _process.

    async def _select_hardware(self, text: str, devices: dict[str, Any]) -> dict[str, Any]:
        """LLM-backed hardware selection. Returns a formatted hardware result dict."""
        if self.llm is None:
            return self._format_hardware_result(text, devices, [], False, "No LLM provider configured.")

        dev_list = devices.get("data", {}).get("devices", []) or []
        payload = {
            "user_request": text,
            "device_discovery": {
                "connected": bool(devices.get("connected")),
                "reason": devices.get("reason", ""),
                "domains": sorted(list(devices.get("domains", set()) or set())),
                "devices": dev_list,
            },
        }

        user_msg = {"role": "user", "content": json.dumps(payload)}
        try:
            response, usage = await self.llm.complete(messages=[user_msg], system=HARDWARE_SELECTION_PROMPT)
            self._accumulate_usage(usage)
            data = json.loads(self._strip_fences(response))
            if not isinstance(data, dict):
                raise ValueError("LLM response is not a JSON object")

            selected: list[dict[str, Any]] = data.get("hardware") or []
            if not isinstance(selected, list):
                selected = []
            can_fulfill = bool(data.get("can_fulfill"))
            fallback_text = str(data.get("result", "")).strip()

            # Self-correction: can_fulfill=true but empty hardware list
            if can_fulfill and not selected:
                correction = {
                    "role": "user",
                    "content": (
                        "Your previous JSON is invalid: can_fulfill=true but hardware is empty. "
                        "Return corrected JSON only. Either provide at least one hardware item "
                        "or set can_fulfill=false."
                    ),
                }
                retry, usage = await self.llm.complete(
                    messages=[user_msg, {"role": "assistant", "content": response}, correction],
                    system=HARDWARE_SELECTION_PROMPT,
                )
                self._accumulate_usage(usage)
                retry_data = json.loads(self._strip_fences(retry))
                if isinstance(retry_data, dict):
                    selected = retry_data.get("hardware") or []
                    if not isinstance(selected, list):
                        selected = []
                    can_fulfill = bool(retry_data.get("can_fulfill"))
                    fallback_text = str(retry_data.get("result", "")).strip()

            if can_fulfill and not selected:
                can_fulfill = False
                fallback_text = "LLM response was inconsistent (can_fulfill=true with empty hardware). Please retry."

            return self._format_hardware_result(text, devices, selected, can_fulfill, fallback_text)

        except Exception as exc:
            logger.error("[%s] Hardware selection failed: %s", self.name, exc, exc_info=True)
            return self._format_hardware_result(text, devices, [], False, f"Hardware selection error: {exc}")


    async def _recommend_hardware(self, text: str, devices: dict[str, Any]) -> dict[str, Any]:
        """Entry point for pure hardware-recommendation requests."""
        connected = bool(devices.get("connected"))
        available_entities = self._available_entity_ids(devices)

        if not connected:
            reason = str(devices.get("reason", "Device discovery unavailable.")).strip()
            return self._format_available_hardware_result(
                text,
                devices,
                [],
                [],
                False,
                reason or "Device discovery unavailable.",
            )

        if self.llm is None:
            return self._format_available_hardware_result(
                text,
                devices,
                [],
                [],
                False,
                "No LLM provider configured.",
            )

        payload = {
            "user_request": text,
            "device_discovery": {
                "connected": connected,
                "reason": devices.get("reason", ""),
                "devices": devices.get("data", {}).get("devices", []) or [],
                "entities": devices.get("data", {}).get("entities", []) or [],
                "floors": devices.get("data", {}).get("floors", []) or [],
                "areas": devices.get("data", {}).get("areas", []) or [],
            },
        }
        user_msg = {"role": "user", "content": json.dumps(payload)}

        try:
            response, usage = await self.llm.complete(
                messages=[user_msg],
                system=HARDWARE_RECOMMENDATION_PROMPT,
            )
            logger.info("[%s] Received hardware recommendation response from LLM.", self.name)
            self._accumulate_usage(usage)
            data = json.loads(self._strip_fences(response))
            if not isinstance(data, dict):
                raise ValueError("LLM response is not a JSON object")

            primary = self._normalize_available_hardware_items(
                data.get("primary_hardware") or [],
                available_entities,
            )
            alternatives = self._normalize_available_hardware_items(
                data.get("alternatives") or [],
                available_entities,
            )
            alternatives = self._filter_hardware_alternatives(primary, alternatives)
            can_fulfill = bool(data.get("can_fulfill"))
            fallback_text = str(data.get("result", "")).strip()

            if can_fulfill and not primary:
                correction = {
                    "role": "user",
                    "content": (
                        "Your previous JSON is invalid: can_fulfill=true but primary_hardware is empty or not grounded in discovered entities. "
                        "Return corrected JSON only. Either provide at least one valid primary_hardware item with discovered entity_ids or set can_fulfill=false."
                    ),
                }
                retry, usage = await self.llm.complete(
                    messages=[user_msg, {"role": "assistant", "content": response}, correction],
                    system=HARDWARE_RECOMMENDATION_PROMPT,
                )
                self._accumulate_usage(usage)
                retry_data = json.loads(self._strip_fences(retry))
                if isinstance(retry_data, dict):
                    primary = self._normalize_available_hardware_items(
                        retry_data.get("primary_hardware") or [],
                        available_entities,
                    )
                    alternatives = self._normalize_available_hardware_items(
                        retry_data.get("alternatives") or [],
                        available_entities,
                    )
                    alternatives = self._filter_hardware_alternatives(primary, alternatives)
                    can_fulfill = bool(retry_data.get("can_fulfill"))
                    fallback_text = str(retry_data.get("result", "")).strip()

            if can_fulfill and not primary:
                can_fulfill = False
                fallback_text = (
                    fallback_text
                    or "No grounded hardware recommendations could be verified from the discovered entities."
                )

            return self._format_available_hardware_result(
                text,
                devices,
                primary,
                alternatives,
                can_fulfill,
                fallback_text,
            )

        except Exception as exc:
            logger.error("[%s] Hardware recommendation failed: %s", self.name, exc, exc_info=True)
            return self._format_available_hardware_result(
                text,
                devices,
                [],
                [],
                False,
                f"Hardware recommendation error: {exc}",
            )


    def _format_available_hardware_result(
        self,
        text: str,
        devices: dict[str, Any],
        primary_hardware: list[dict[str, Any]],
        alternatives: list[dict[str, Any]],
        can_fulfill: bool,
        fallback_text: str = "",
    ) -> dict[str, Any]:
        connected = bool(devices.get("connected"))
        has_primary = bool(primary_hardware)

        lines = [f"Can be done with existing hardware: {'yes' if can_fulfill and has_primary else 'no'}." ]
        if has_primary:
            lines.append("Primary hardware:")
            lines.extend(self._hardware_summary_lines(primary_hardware))
            if alternatives:
                lines.append("Alternatives:")
                lines.extend(self._hardware_summary_lines(alternatives))
            if can_fulfill:
                lines.append("Recommendations are grounded only in currently discovered Home Assistant entities.")
            elif fallback_text:
                lines.append(fallback_text)
            else:
                lines.append("The selected hardware covers only part of the request based on currently discovered Home Assistant entities.")
        else:
            lines.append(
                fallback_text
                or (
                    "No combination of the currently discovered hardware can satisfy this request."
                    if connected
                    else "Device discovery unavailable."
                )
            )
            if alternatives:
                lines.append("Other related available hardware:")
                lines.extend(self._hardware_summary_lines(alternatives))

        return {
            "can_fulfill": bool(can_fulfill and has_primary),
            "task": text,
            "request": text,
            "hardware": primary_hardware,
            "primary_hardware": primary_hardware,
            "alternatives": alternatives,
            "based_on_available_hardware": connected,
            "result": "\n".join(lines),
            "device_discovery": {"connected": connected, "reason": devices.get("reason", "")},
        }


    def _format_hardware_result(
        self,
        text: str,
        devices: dict[str, Any],
        hardware: list[dict[str, Any]],
        can_fulfill: bool,
        fallback_text: str = "",
    ) -> dict[str, Any]:
        connected = bool(devices.get("connected"))

        if not can_fulfill or not hardware:
            cannot = fallback_text or (
                "I found Home Assistant devices, but none are relevant to this automation request."
                if connected
                else "HOME_ASSISTANT_URL or HA_TOKEN not configured; cannot discover devices."
            )
            return {
                "can_fulfill": False,
                "task": text,
                "request": text,
                "hardware": [],
                "result": cannot,
                "device_discovery": {"connected": connected, "reason": devices.get("reason", "")},
            }

        lines = ["Best hardware for this automation:"]
        for rec in hardware:
            line = f"- {rec.get('hardware', '?')} ({rec.get('protocol', 'N/A')}) — {rec.get('why', '')}"
            entities_list = rec.get("required_entities") or []
            if connected and isinstance(entities_list, list) and entities_list:
                shown = [str(e) for e in entities_list[:3]]
                line += f"  Available: {', '.join(shown)}"
            lines.append(line)

        if connected:
            lines.append("Based on currently discovered Home Assistant entities.")
        else:
            lines.append(f"Device discovery unavailable: {devices.get('reason', 'N/A')}.")

        return {
            "can_fulfill": True,
            "task": text,
            "request": text,
            "hardware": hardware,
            "result": "\n".join(lines),
            "device_discovery": {"connected": connected, "reason": devices.get("reason", "")},
        }


    # ── Automation creation ───────────────────────────────────────────────────

    async def _create_automation(
        self,
        text: str,
        entities: list[str],
        hardware: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            generated = await self._generate_automation(text, entities, hardware)
            if not generated.get("can_create"):
                return {
                    "can_create": False,
                    "inserted": False,
                    "result": generated.get("result", "Could not create automation."),
                    "automation": {},
                }

            automation = generated["automation"]
            insert_result = await self._insert_automation(automation)
            if not insert_result.get("inserted"):
                return {
                    "can_create": True,
                    "inserted": False,
                    "result": (
                        f"Automation plan created but failed to insert into Home Assistant: "
                        f"{insert_result.get('error', 'unknown error')}"
                    ),
                    "automation": automation,
                }

            return {
                "can_create": True,
                "inserted": True,
                "result": f"Automation '{automation.get('name', 'Generated automation')}' created in Home Assistant.",
                "automation": automation,
                "home_assistant": insert_result.get("response"),
            }

        except Exception as exc:
            logger.error("[%s] Automation creation failed: %s", self.name, exc, exc_info=True)
            return {
                "can_create": False,
                "inserted": False,
                "result": f"Failed to create automation: {exc}",
                "automation": {},
            }


    async def _generate_automation(
        self,
        text: str,
        entities: list[str],
        hardware: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.llm is None:
            return {"can_create": False, "result": "No LLM provider configured.", "automation": {}}

        payload = {
            "user_request": text,
            "selected_entities": entities,
            "hardware_context": hardware,
        }
        response, usage = await self.llm.complete(
            messages=[{"role": "user", "content": json.dumps(payload)}],
            system=AUTOMATION_CREATION_PROMPT,
        )
        self._accumulate_usage(usage)
        data = json.loads(self._strip_fences(response))
        if not isinstance(data, dict):
            raise ValueError("LLM response is not a JSON object")

        can_create = bool(data.get("can_create"))
        automation = data.get("automation") or {}
        result_text = str(data.get("result", "")).strip()

        if not can_create:
            return {
                "can_create": False,
                "result": result_text or "Not enough information to safely create an automation.",
                "automation": {},
            }

        if not isinstance(automation, dict):
            raise ValueError("automation must be a JSON object")

        error = self._validate_automation(automation)
        if error:
            raise ValueError(error)

        return {
            "can_create": True,
            "result": result_text or "Automation ready.",
            "automation": {
                "name": automation.get("name", "Generated automation"),
                "description": automation.get("description", "Generated by home-assistant-agent"),
                "trigger": automation.get("trigger", []),
                "condition": automation.get("condition", []),
                "action": automation.get("action", []),
                "mode": automation.get("mode", "single"),
            },
        }


    async def _insert_automation(self, automation: dict[str, Any]) -> dict[str, Any]:
        if not self.ha_url or not self.ha_token:
            return {"inserted": False, "error": "HA_URL or HA_TOKEN not configured"}
        try:
            response = await create_automation_via_rest(self.ha_url, self.ha_token, automation)
            return {"inserted": True, "response": response}
        except Exception as exc:
            return {"inserted": False, "error": str(exc)}


    # ── Automation listing ────────────────────────────────────────────────────

    async def _get_automations_brief(self) -> list[dict[str, Any]]:
        """Return a brief list (id, name, description) with caching."""
        now = time.time()
        cached = self._automation_cache.get("data")
        if cached is not None and now - float(self._automation_cache.get("timestamp", 0.0)) < self._automation_cache_ttl:
            return cached

        if not self.ha_url or not self.ha_token:
            self._automation_cache = {"timestamp": now, "data": []}
            return []

        try:
            full = await get_automations(self.ha_url, self.ha_token)
            brief = [
                {
                    "id": a.get("id", "") or a.get("automation_id", ""),
                    "name": a.get("alias", "") or a.get("name", ""),
                    "description": a.get("description", ""),
                }
                for a in (full or [])
                if isinstance(a, dict)
            ]
            self._automation_cache = {"timestamp": now, "data": brief}
            return brief
        except Exception as exc:
            logger.warning("[%s] Could not fetch automations: %s", self.name, exc)
            self._automation_cache = {"timestamp": now, "data": []}
            return []
        

    def _list_automations(self, automations: list[dict[str, Any]]) -> dict[str, Any]:
        if not automations:
            suffix = " (or Home Assistant is not configured)." if not self.ha_url else "."
            return {"result": f"No automations found in Home Assistant{suffix}", "automations": []}

        lines = [f"Found {len(automations)} automation(s) in Home Assistant:"]
        for i, a in enumerate(automations, 1):
            name = a.get("name") or "(unnamed)"
            desc = a.get("description") or ""
            line = f"{i}. {name}"
            if desc:
                line += f" — {desc}"
            lines.append(line)

        return {"result": "\n".join(lines), "automations": automations}


    # ── Automation deletion ───────────────────────────────────────────────────

    async def _delete_automation(
        self,
        text: str,
        automations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not automations:
            return {"result": "No automations found in Home Assistant to delete.", "deleted": False}
        if self.llm is None:
            return {"result": "No LLM provider configured.", "deleted": False}

        payload = {"user_request": text, "automations": automations}
        try:
            response, usage = await self.llm.complete(
                messages=[{"role": "user", "content": json.dumps(payload)}],
                system=HA_DELETE_CONFIRM_PROMPT,
            )
            self._accumulate_usage(usage)
            data = json.loads(self._strip_fences(response))
        except Exception as exc:
            return {"result": f"Could not identify automation to delete: {exc}", "deleted": False}

        if not isinstance(data, dict):
            return {"result": "Could not identify which automation to delete.", "deleted": False}
        if not data.get("found"):
            return {
                "result": str(data.get("result", "Could not identify which automation to delete.")),
                "deleted": False,
            }

        automation_id = str(data.get("automation_id", "")).strip()
        automation_name = str(data.get("automation_name", "")).strip()

        if not automation_id:
            return {"result": "Could not determine automation ID to delete.", "deleted": False}
        if not self.ha_url or not self.ha_token:
            return {"result": "HA_URL or HA_TOKEN not configured.", "deleted": False}

        try:
            success = await delete_automation(self.ha_url, self.ha_token, automation_id)
            if success:
                self._automation_cache = {"timestamp": 0.0, "data": None}  # invalidate
                return {
                    "result": f"Automation '{automation_name}' deleted successfully.",
                    "deleted": True,
                    "automation_id": automation_id,
                    "automation_name": automation_name,
                }
            return {
                "result": f"Failed to delete automation '{automation_name}'. Home Assistant returned an error.",
                "deleted": False,
            }
        except Exception as exc:
            return {"result": f"Error deleting automation: {exc}", "deleted": False}


    # ── Automation editing ────────────────────────────────────────────────────

    async def _identify_automation(
        self,
        text: str,
        automations: list[dict[str, Any]],
    ) -> tuple[str, str]:
        """Identify which automation the user wants to edit based on their request and the list of automations."""
        ident_payload = {"user_request": text, "automations": automations}
        try:
            ident_response, usage = await self.llm.complete(
                messages=[{"role": "user", "content": json.dumps(ident_payload)}],
                system=HA_IDENTIFY_AUTOMATION_PROMPT,
            )
            self._accumulate_usage(usage)
            ident_data = json.loads(self._strip_fences(ident_response))
        except Exception as exc:
            raise AutomationEditError(f"Could not identify automation to edit: {exc}") from exc

        if not isinstance(ident_data, dict):
            raise AutomationEditError("Could not identify which automation to edit.")
        if not ident_data.get("found"):
            raise AutomationEditError(str(ident_data.get("result", "Could not identify which automation to edit.")))

        automation_id = str(ident_data.get("automation_id", "")).strip()
        automation_name = str(ident_data.get("automation_name", "")).strip()

        if not automation_id:
            raise AutomationEditError("Could not determine the automation ID to edit.")

        return automation_id, automation_name


    async def _get_automation_config(self, automation_id: str, automation_name: str) -> dict[str, Any]:
        """Fetch the full automation config for a given automation ID."""
        try:
            full_list = await get_automations(self.ha_url, self.ha_token)
            match = next(
                (
                    a for a in (full_list or [])
                    if isinstance(a, dict)
                    and (a.get("id") == automation_id or a.get("alias") == automation_name)
                ),
                None,
            )
            if isinstance(match, dict):
                return match
            return {}
        except Exception as exc:
            logger.warning("[%s] Could not fetch full automation config: %s", self.name, exc)
            return {}


    async def _generate_modified_automation_config(
        self,
        text: str,
        existing_config: dict[str, Any],
        entity_ids: list[str],
    ) -> dict[str, Any]:
        """Generate the updated automation config from the user's edit request."""
        edit_payload = {
            "user_request": text,
            "existing_automation": existing_config,
            "available_entities": entity_ids[:100],
        }
        try:
            edit_response, usage = await self.llm.complete(
                messages=[{"role": "user", "content": json.dumps(edit_payload)}],
                system=HA_EDIT_AUTOMATION_PROMPT,
            )
            self._accumulate_usage(usage)
            edit_data = json.loads(self._strip_fences(edit_response))
        except Exception as exc:
            raise AutomationEditError(f"LLM could not generate updated automation: {exc}") from exc

        if not isinstance(edit_data, dict):
            raise AutomationEditError("Invalid generated automation config.")
        if not edit_data.get("can_edit"):
            raise AutomationEditError(str(edit_data.get("result", "Automation cannot be edited.")))
        updated_automation = edit_data.get("automation") or {}
        if not isinstance(updated_automation, dict):
            raise AutomationEditError("Generated automation config must be an object.")
        return updated_automation


    async def _edit_automation(
        self,
        text: str,
        automations: list[dict[str, Any]],
        devices: dict[str, Any],
    ) -> dict[str, Any]:
        if not automations:
            return {"result": "No automations found in Home Assistant to edit.", "edited": False}
        if self.llm is None:
            return {"result": "No LLM provider configured.", "edited": False}
        if not self.ha_url or not self.ha_token:
            return {"result": "HA_URL or HA_TOKEN not configured.", "edited": False}

        # Step 1 — identify which automation the user wants to edit
        try:
            automation_id, automation_name = await self._identify_automation(text, automations)
        except AutomationEditError as exc:
            return {"result": str(exc), "edited": False}

        # Fetch the full automation config for context
        existing_config: dict[str, Any] = {"id": automation_id, "alias": automation_name}
        fetched_config = await self._get_automation_config(automation_id, automation_name)
        if fetched_config:
            existing_config = fetched_config
        else:
            logger.warning(
                "[%s] Could not fetch full automation config for automation_id: %s, automation_name: %s",
                self.name,
                automation_id,
                automation_name,
            )

        # Build flat entity list for context (cap to avoid huge prompts)
        entity_ids = self._entity_ids_from_devices(devices)

        # Step 2 — LLM generates the updated automation
        try:
            updated_automation = await self._generate_modified_automation_config(text, existing_config, entity_ids)
        except AutomationEditError as exc:
            logger.warning("[%s] Could not generate updated automation: %s", self.name, exc)
            return {"result": str(exc), "edited": False}

        error = self._validate_automation(updated_automation)
        if error:
            return {"result": f"Updated automation is invalid: {error}", "edited": False}

        try:
            await update_automation(self.ha_url, self.ha_token, automation_id, updated_automation)
            self._automation_cache = {"timestamp": 0.0, "data": None}  # invalidate
            return {
                "result": f"Automation '{automation_name}' updated successfully.",
                "edited": True,
                "automation_id": automation_id,
                "automation_name": automation_name,
                "automation": updated_automation,
            }
        except Exception as exc:
            return {"result": f"Error updating automation: {exc}", "edited": False}


    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _entity_ids_from_devices(devices: dict[str, Any]) -> list[str]:
        return [
            e.get("entity_id")
            for e in devices.get("data", {}).get("entities", []) or []
            if e.get("entity_id")
        ]


    @staticmethod
    def _extract_payload(payload: Any) -> tuple[str, list[str], list[dict[str, Any]]]:
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("task") or "").strip()
            entities = payload.get("entities") or []
            hardware = payload.get("hardware") or []
            if not isinstance(entities, list):
                entities = []
            if not isinstance(hardware, list):
                hardware = []
            entities = [str(e).strip() for e in entities if str(e).strip()]
            return text, entities, hardware
        return str(payload), [], []


    @staticmethod
    def _extract_task_id(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict) and isinstance(payload.get("task"), str):
            return payload["task"]
        return fallback


    @staticmethod
    def _strip_fences(text: str) -> str:
        """Remove markdown code fences from an LLM response.

        LLMs often wrap JSON output in triple-backtick fences (e.g. ```json ... ```).
        This strips the opening fence and optional language tag as well as the closing
        fence, returning only the inner content. If no fences are present the text is
        returned unchanged (after stripping surrounding whitespace).
        """
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*", "", cleaned).strip()
            cleaned = re.sub(r"```$", "", cleaned).strip()
        return cleaned


    @staticmethod
    def _validate_automation(automation: dict[str, Any]) -> str | None:
        if not isinstance(automation.get("name"), str) or not automation["name"].strip():
            return "automation.name is required"
        if not isinstance(automation.get("trigger"), list) or not automation["trigger"]:
            return "automation.trigger must be a non-empty list"
        if not isinstance(automation.get("action"), list) or not automation["action"]:
            return "automation.action must be a non-empty list"
        if not isinstance(automation.get("condition", []), list):
            return "automation.condition must be a list"
        if not isinstance(automation.get("mode", "single"), str) or not automation.get("mode", "single").strip():
            return "automation.mode must be a non-empty string"
        return None


    @staticmethod
    def _available_entity_ids(devices: dict[str, Any]) -> set[str]:
        """Extract the flat set of all entity IDs from a device-discovery result.

        Walks ``devices["data"]["entities"]`` and collects every non-empty
        ``entity_id`` string. The resulting set is used as the ground-truth
        allowlist when normalizing LLM hardware recommendations, so that any
        entity ID the LLM invented but that is not present here gets discarded.
        """
        available: set[str] = set()
        for entity in devices.get("data", {}).get("entities", []) or []:
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("entity_id", "")).strip()
            if entity_id:
                available.add(entity_id)
        return available


    @staticmethod
    def _normalize_available_hardware_items(
        items: list[dict[str, Any]],
        available_entities: set[str],
    ) -> list[dict[str, Any]]:
        """Validate and sanitize raw LLM hardware items against discovered HA entities.

        For each item returned by the LLM:
        - Keeps only ``required_entities`` that exist in ``available_entities``,
          discarding hallucinated entity IDs.
        - Drops the item entirely if no valid entities remain after filtering.
        - Derives ``required_domains`` from entity ID prefixes when the LLM omitted them.
        - Drops items that still lack a hardware name or resolvable domain.
        - Deduplicates by ``(hardware_name, entity_ids)`` to prevent repeated entries.
        - Normalises the output shape to a consistent set of fields.

        The result is a clean, grounded list where every recommendation is backed
        by real, currently-discovered Home Assistant entities.
        """
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()

        for item in items:
            if not isinstance(item, dict):
                continue

            entity_ids: list[str] = []
            for entity_id in item.get("required_entities", []) or []:
                normalized_entity = str(entity_id).strip()
                if (
                    normalized_entity
                    and normalized_entity in available_entities
                    and normalized_entity not in entity_ids
                ):
                    entity_ids.append(normalized_entity)

            if not entity_ids:
                continue

            hardware_name = str(item.get("hardware", "")).strip()
            why = str(item.get("why", "")).strip()
            protocol = str(item.get("protocol", "")).strip() or "N/A"
            required_domains = [
                str(domain).strip()
                for domain in item.get("required_domains", []) or []
                if str(domain).strip()
            ]
            if not required_domains:
                required_domains = sorted({entity_id.split(".", 1)[0] for entity_id in entity_ids if "." in entity_id})

            if not hardware_name or not required_domains:
                continue

            identity = (hardware_name.lower(), tuple(entity_ids))
            if identity in seen:
                continue
            seen.add(identity)
            normalized.append(
                {
                    "hardware": hardware_name,
                    "why": why,
                    "protocol": protocol,
                    "required_domains": required_domains,
                    "required_entities": entity_ids,
                    **(
                        {"alternative_to": str(item.get("alternative_to", "")).strip()}
                        if str(item.get("alternative_to", "")).strip()
                        else {}
                    ),
                }
            )

        return normalized


    @staticmethod
    def _filter_hardware_alternatives(
        primary_hardware: list[dict[str, Any]],
        alternatives: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove alternatives that are not genuine substitutes for primary hardware.

        An alternative survives only if all four conditions hold:
        - It has at least one ``required_entity``.
        - Its ``alternative_to`` field is non-empty and matches an entity ID that
          appears in ``primary_hardware`` — linking it to a specific primary item.
        - None of its own ``required_entities`` overlap with the primary entity set,
          preventing the LLM from re-listing a primary item as its own alternative.
        - Its ``(hardware_name, entity_ids)`` identity has not been seen before,
          removing duplicates.

        The result is a deduplicated list of alternatives that each offer a distinct,
        non-overlapping swap for one of the recommended primary hardware items.
        """
        primary_entities = {
            str(entity_id).strip()
            for item in primary_hardware
            if isinstance(item, dict)
            for entity_id in item.get("required_entities", []) or []
            if str(entity_id).strip()
        }
        filtered: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()

        for item in alternatives:
            if not isinstance(item, dict):
                continue
            alternative_to = str(item.get("alternative_to", "")).strip()
            entity_ids = [
                str(entity_id).strip()
                for entity_id in item.get("required_entities", []) or []
                if str(entity_id).strip()
            ]
            if (
                not entity_ids
                or not alternative_to
                or alternative_to not in primary_entities
                or any(entity_id in primary_entities for entity_id in entity_ids)
            ):
                continue
            identity = (str(item.get("hardware", "")).strip().lower(), tuple(entity_ids))
            if identity in seen:
                continue
            seen.add(identity)
            filtered.append(item)

        return filtered


    @staticmethod
    def _hardware_summary_lines(items: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entities = [str(entity_id) for entity_id in item.get("required_entities", []) or [] if str(entity_id)]
            line = f"- {item.get('hardware', '?')} ({item.get('protocol', 'N/A')})"
            alternative_to = str(item.get("alternative_to", "")).strip()
            if alternative_to:
                line += f" [alternative to {alternative_to}]"
            why = str(item.get("why", "")).strip()
            if why:
                line += f" — {why}"
            if entities:
                line += f" Available: {', '.join(entities[:3])}"
            lines.append(line)
        return lines


    @staticmethod
    def _extract_entity_ids_from_hardware(hardware_result: dict[str, Any]) -> list[str]:
        seen: set[str] = set()
        entities: list[str] = []
        for item in hardware_result.get("hardware", []) or []:
            if not isinstance(item, dict):
                continue
            for eid in item.get("required_entities", []) or []:
                normalized = str(eid).strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    entities.append(normalized)
        return entities


    @staticmethod
    def _extract_entity_ids(text: str) -> list[str]:
        seen: set[str] = set()
        entity_ids: list[str] = []
        for match in re.finditer(r"\b[a-z_][a-z0-9_]*\.[a-z0-9_]+\b", text.lower()):
            entity_id = match.group(0)
            if entity_id not in seen:
                seen.add(entity_id)
                entity_ids.append(entity_id)
        return entity_ids
