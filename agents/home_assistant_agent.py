"""
HomeAssistantAgent - Unified Home Assistant agent.

Handles all HA operations in a single agent:
  - recommend_hardware    : advise which devices/entities are needed
  - create_automation     : build and insert a new automation via REST
  - delete_automation     : remove an existing automation
  - edit_automation       : update an existing automation
  - list_automations      : enumerate all automations
  - list_areas            : enumerate Home Assistant areas
  - list_devices          : enumerate Home Assistant devices
  - list_entities         : enumerate Home Assistant entities

Intent is classified with a cheap single-word LLM call, then the
appropriate code path runs.  Complex operations (create, edit) use up
to two additional LLM calls internally; simpler ones (list, delete) use
one.  All HA communication goes through ha_helper.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from ..config import CONFIG

from ..core.actor import Message, MessageType
from ..core.integrations.home_assistant.ha_helper import (
    create_automation_via_rest,
    delete_automation,
    fetch_devices_entities_with_location,
    get_areas,
    get_automations,
    get_devices,
    get_entities,
    update_automation,
)
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

HA_ACTION_CLASSIFICATION_PROMPT = """Classify a Home Assistant user request.
Output exactly one of these strings — nothing else, no punctuation:

recommend_hardware
create_automation
delete_automation
edit_automation
list_automations
list_areas
list_devices
list_entities

Guidelines:
- recommend_hardware  → user wants hardware/device suggestions or compatibility info
- create_automation   → user wants to create/add/build/make a new automation
- delete_automation   → user wants to delete/remove/disable an existing automation
- edit_automation     → user wants to update/change/rename/modify an existing automation
- list_automations    → user wants to see/list/show existing automations
- list_areas          → user wants to see/list/show areas
- list_devices        → user wants to see/list/show devices
- list_entities       → user wants to see/list/show entities
"""

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
- If can_fulfill=false because hardware is missing, result MUST explicitly list what is missing.
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

AUTOMATION_CREATION_PROMPT = """You are a Home Assistant automation authoring specialist.

Task:
- Build a valid Home Assistant automation object from the user request.
- Use provided entity_ids as first priority for trigger/action targets.
- Return strict JSON only.

Input JSON keys:
- user_request: string
- selected_entities: [entity_id]
- hardware_context: optional list from hardware selection

Rules:
- Prefer entities from selected_entities.
- Include at least one trigger and one action.
- If there is not enough information to build a safe automation, return can_create=false.
- Keep conditions minimal.
- Use mode="single" unless request clearly needs another mode.
- Never return markdown.

Output JSON schema:
{
  "can_create": boolean,
  "result": string,
  "automation": {
    "name": string,
    "description": string,
    "trigger": [object],
    "condition": [object],
    "action": [object],
    "mode": string
  }
}
"""

HA_IDENTIFY_AUTOMATION_PROMPT = """You identify which Home Assistant automation a user is referring to.

Input JSON:
- user_request: string
- automations: [{ "id": string, "name": string, "description": string }]

Output strict JSON:
{
  "found": boolean,
  "automation_id": string,
  "automation_name": string,
  "result": string
}

Rules:
- Match by name (fuzzy matching is ok, prefer exact match).
- If found, set automation_id and automation_name from the matched automation.
- If not found or ambiguous (multiple plausible matches), set found=false and explain in result.
- If automations list is empty, set found=false.
"""

HA_DELETE_CONFIRM_PROMPT = """You identify which Home Assistant automation the user wants to delete.

Input JSON:
- user_request: string
- automations: [{ "id": string, "name": string, "description": string }]

Output strict JSON:
{
  "found": boolean,
  "automation_id": string,
  "automation_name": string,
  "result": string
}

Rules:
- Match by name (fuzzy ok, prefer exact).
- If not found or ambiguous, set found=false and explain in result.
- If found=true, automation_id must be the exact "id" field from the matched automation entry.
"""

HA_EDIT_AUTOMATION_PROMPT = """You update an existing Home Assistant automation based on a change request.

Input JSON:
- user_request: string (what to change)
- existing_automation: object (current full automation config)
- available_entities: [string] (entity IDs available in HA)

Output strict JSON:
{
  "can_edit": boolean,
  "result": string,
  "automation": {
    "name": string,
    "description": string,
    "trigger": [object],
    "condition": [object],
    "action": [object],
    "mode": string
  }
}

Rules:
- Only change what the user explicitly requested. Keep everything else identical.
- Prefer entities from available_entities when applicable.
- If the request is unclear, unsafe, or impossible to apply, set can_edit=false and explain in result.
- Always return a complete automation object (not a diff), even if only one field changed.
"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class HomeAssistantAgent(LLMAgent):
    """Unified Home Assistant agent: hardware recommendations and automation CRUD."""

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

    
    # ── Cost tracking helper ─────────────────────────────────────────────────

    def _accumulate_usage(self, usage: dict) -> None:
        """Add token counts and cost from one llm.complete() call to running totals."""
        if not isinstance(usage, dict):
            return
        self.total_input_tokens  += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd      += usage.get("cost_usd", 0.0)

    # ── Public entry points ──────────────────────────────────────────────────

    async def chat(self, user_message: str) -> str:
        """Direct entry point used by CLI when addressing this agent."""
        result = await self._process(user_message)
        return str(result.get("result", ""))

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

        self.metrics.tasks_completed += 1
        if msg.sender_id:
            await self.send(msg.sender_id, MessageType.RESULT, result)

    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def _process(self, text: str) -> dict[str, Any]:
        """Classify intent then route to the appropriate handler."""
        action = await self._classify_action(text)

        if action == "list_areas":
            return await self._list_areas()

        if action == "list_devices":
            return await self._list_devices()

        if action == "list_entities":
            return await self._list_entities()

        if action == "list_automations":
            automations = await self._get_automations_brief()
            return self._list_automations(automations)

        if action == "delete_automation":
            automations = await self._get_automations_brief()
            return await self._delete_automation(text, automations)

        if action == "edit_automation":
            automations = await self._get_automations_brief()
            devices = await self._get_devices()
            return await self._edit_automation(text, automations, devices)

        if action == "recommend_hardware":
            devices = await self._get_devices()
            return await self._recommend_hardware(text, devices)

        # Default: create_automation — hardware selection then automation generation.
        devices = await self._get_devices()
        hardware_result = await self._select_hardware(text, devices)
        if not hardware_result.get("can_fulfill"):
            return hardware_result

        entities = self._extract_entity_ids_from_hardware(hardware_result)
        return await self._create_automation(text, entities, hardware_result.get("hardware", []))

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
        }

        if self.llm is None:
            return self._classify_action_heuristic(text)

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

        return self._classify_action_heuristic(text)

    @staticmethod
    def _classify_action_heuristic(text: str) -> str:
        lower = text.lower()
        if any(w in lower for w in ("list areas", "show areas", "show me areas", "what areas")):
            return "list_areas"
        if any(w in lower for w in ("list devices", "show devices", "show me devices", "what devices")):
            return "list_devices"
        if any(w in lower for w in ("list entities", "show entities", "show me entities", "what entities")):
            return "list_entities"
        if any(w in lower for w in ("list", "show me", "show all", "what automations", "what are my automations")):
            return "list_automations"
        if any(w in lower for w in ("delete", "remove automation", "disable automation")):
            return "delete_automation"
        if any(w in lower for w in ("edit", "update automation", "change automation", "modify automation", "rename automation")):
            return "edit_automation"
        if any(w in lower for w in ("hardware", "what device", "what sensor", "what do i need", "compatible with")):
            return "recommend_hardware"
        return "create_automation"

    # ── Device discovery ─────────────────────────────────────────────────────

    async def _get_devices(self) -> dict[str, Any]:
        now = time.time()
        cached = self._device_cache.get("data")
        if cached is not None and now - float(self._device_cache.get("timestamp", 0.0)) < self._device_cache_ttl:
            return cached

        if not self.ha_url or not self.ha_token:
            data: dict[str, Any] = {
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
            domains: set[str] = set()
            for device in devices:
                for entity in device.get("entities", []) or []:
                    eid = str(entity.get("entity_id", ""))
                    if "." in eid:
                        domains.add(eid.split(".", 1)[0])
            data = {"connected": True, "domains": domains, "devices": devices, "reason": ""}
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

    # ── Hardware selection ────────────────────────────────────────────────────

    async def _select_hardware(self, text: str, devices: dict[str, Any]) -> dict[str, Any]:
        """LLM-backed hardware selection. Returns a formatted hardware result dict."""
        if self.llm is None:
            return self._format_hardware_result(text, devices, [], False, "No LLM provider configured.")

        dev_list = devices.get("devices", []) or []
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
        return await self._select_hardware(text, devices)

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

        if not isinstance(data, dict) or not data.get("found"):
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

        # Step 1 — identify which automation the user wants to edit
        ident_payload = {"user_request": text, "automations": automations}
        try:
            ident_response, usage = await self.llm.complete(
                messages=[{"role": "user", "content": json.dumps(ident_payload)}],
                system=HA_IDENTIFY_AUTOMATION_PROMPT,
            )
            self._accumulate_usage(usage)
            ident_data = json.loads(self._strip_fences(ident_response))
        except Exception as exc:
            return {"result": f"Could not identify automation to edit: {exc}", "edited": False}

        if not isinstance(ident_data, dict) or not ident_data.get("found"):
            return {
                "result": str(ident_data.get("result", "Could not identify which automation to edit.")),
                "edited": False,
            }

        automation_id = str(ident_data.get("automation_id", "")).strip()
        automation_name = str(ident_data.get("automation_name", "")).strip()

        if not automation_id:
            return {"result": "Could not determine the automation ID to edit.", "edited": False}

        # Fetch the full automation config for context
        existing_config: dict[str, Any] = {"id": automation_id, "alias": automation_name}
        if self.ha_url and self.ha_token:
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
                if match:
                    existing_config = match
            except Exception as exc:
                logger.warning("[%s] Could not fetch full automation config: %s", self.name, exc)

        # Build flat entity list for context (cap to avoid huge prompts)
        entity_ids = [
            e.get("entity_id")
            for d in devices.get("devices", [])
            for e in d.get("entities", [])
            if e.get("entity_id")
        ]

        # Step 2 — LLM generates the updated automation
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
            return {"result": f"LLM could not generate updated automation: {exc}", "edited": False}

        if not isinstance(edit_data, dict) or not edit_data.get("can_edit"):
            return {
                "result": str(edit_data.get("result", "Could not update automation.")),
                "edited": False,
            }

        updated_automation = edit_data.get("automation") or {}
        error = self._validate_automation(updated_automation)
        if error:
            return {"result": f"Updated automation is invalid: {error}", "edited": False}

        if not self.ha_url or not self.ha_token:
            return {"result": "HA_URL or HA_TOKEN not configured.", "edited": False}

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
