"""
HomeAssistantAutomationAgent - Builds and inserts Home Assistant automations.
This agent consumes the user request and pre-selected entities from another agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from ..core.actor import Message, MessageType
from ..core.integrations.home_assistant.ha_helper import create_automation_via_rest
from .llm_agent import LLMAgent, LLMProvider

logger = logging.getLogger(__name__)

AUTOMATION_CREATION_PROMPT = """You are a Home Assistant automation authoring specialist.

Task:
- Build a valid Home Assistant automation object from the user request.
- Use provided entity_ids as first priority for trigger/action targets.
- Return strict JSON only.

Input JSON keys:
- user_request: string
- selected_entities: [entity_id]
- hardware_context: optional list from another agent

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


class HomeAssistantAutomationAgent(LLMAgent):
    """Create and insert Home Assistant automations via REST API."""

    def __init__(self, llm_provider: LLMProvider | None = None, **kwargs):
        kwargs.setdefault("name", "home-assistant-automation")
        kwargs.setdefault("system_prompt", AUTOMATION_CREATION_PROMPT)
        super().__init__(llm_provider=llm_provider, **kwargs)
        self.ha_url = (os.getenv("HOME_ASSISTANT_URL") or os.getenv("HA_URL") or "").strip()
        self.ha_token = (os.getenv("HOME_ASSISTANT_TOKEN") or os.getenv("HA_TOKEN") or "").strip()

    async def chat(self, user_message: str) -> str:
        result = await self._create_and_insert(
            request_text=user_message,
            selected_entities=[],
            hardware_context=[],
        )
        return str(result.get("result", ""))

    async def handle_message(self, msg: Message):
        if msg.type != MessageType.TASK:
            return

        request_text, selected_entities, hardware_context = self._extract_payload(msg.payload)
        result = await self._create_and_insert(
            request_text=request_text,
            selected_entities=selected_entities,
            hardware_context=hardware_context,
        )
        if isinstance(result, dict):
            result.setdefault("task", self._extract_task_id(msg.payload, request_text))

        self.metrics.tasks_completed += 1
        if msg.sender_id:
            await self.send(msg.sender_id, MessageType.RESULT, result)

    @staticmethod
    def _extract_task_id(payload: Any, fallback: str) -> str:
        if isinstance(payload, dict) and isinstance(payload.get("task"), str):
            return payload["task"]
        return fallback

    @staticmethod
    def _extract_payload(payload: Any) -> tuple[str, list[str], list[dict[str, Any]]]:
        if isinstance(payload, dict):
            request_text = str(payload.get("text") or payload.get("task") or "").strip()
            selected_entities = payload.get("entities") or []
            hardware_context = payload.get("hardware") or []
            if not isinstance(selected_entities, list):
                selected_entities = []
            if not isinstance(hardware_context, list):
                hardware_context = []
            normalized_entities = [str(entity_id).strip() for entity_id in selected_entities if str(entity_id).strip()]
            return request_text, normalized_entities, hardware_context

        return str(payload), [], []

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
        trigger = automation.get("trigger")
        action = automation.get("action")
        if not isinstance(trigger, list) or not trigger:
            return "automation.trigger must be a non-empty list"
        if not isinstance(action, list) or not action:
            return "automation.action must be a non-empty list"
        condition = automation.get("condition", [])
        if not isinstance(condition, list):
            return "automation.condition must be a list"
        mode = automation.get("mode", "single")
        if not isinstance(mode, str) or not mode.strip():
            return "automation.mode must be a non-empty string"
        return None

    async def _create_with_llm(
        self,
        request_text: str,
        selected_entities: list[str],
        hardware_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.llm is None:
            return {
                "can_create": False,
                "result": "Cannot create automation because no LLM provider is configured.",
                "automation": {},
            }

        payload = {
            "user_request": request_text,
            "selected_entities": selected_entities,
            "hardware_context": hardware_context,
        }

        response, _ = await self.llm.complete(
            messages=[{"role": "user", "content": json.dumps(payload)}],
            system=self.system_prompt,
        )
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
            raise ValueError("automation must be an object")

        validation_error = self._validate_automation(automation)
        if validation_error:
            raise ValueError(validation_error)

        return {
            "can_create": True,
            "result": result_text or "Automation created.",
            "automation": {
                "name": automation.get("name", "Generated automation"),
                "description": automation.get("description", "Generated by home-assistant-automation agent"),
                "trigger": automation.get("trigger", []),
                "condition": automation.get("condition", []),
                "action": automation.get("action", []),
                "mode": automation.get("mode", "single"),
            },
        }

    async def _insert_automation(self, automation: dict[str, Any]) -> dict[str, Any]:
        if not self.ha_url or not self.ha_token:
            return {
                "inserted": False,
                "error": "HOME_ASSISTANT_URL/HA_URL or HOME_ASSISTANT_TOKEN/HA_TOKEN is not configured",
            }

        payload = {
            "name": automation["name"],
            "description": automation.get("description", ""),
            "trigger": automation["trigger"],
            "condition": automation.get("condition", []),
            "action": automation["action"],
            "mode": automation.get("mode", "single"),
        }

        result = await create_automation_via_rest(self.ha_url, self.ha_token, payload)
        return {"inserted": True, "response": result}

    async def _create_and_insert(
        self,
        request_text: str,
        selected_entities: list[str],
        hardware_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            generated = await self._create_with_llm(request_text, selected_entities, hardware_context)
            if not generated.get("can_create"):
                return {
                    "can_create": False,
                    "inserted": False,
                    "result": generated.get("result", "Could not create automation."),
                    "automation": {},
                }

            automation = generated["automation"]
            inserted = await self._insert_automation(automation)
            if not inserted.get("inserted"):
                return {
                    "can_create": True,
                    "inserted": False,
                    "result": (
                        f"Created automation plan but failed to insert into Home Assistant: {inserted.get('error', 'unknown error')}"
                    ),
                    "automation": automation,
                }

            return {
                "can_create": True,
                "inserted": True,
                "result": f"Automation '{automation.get('name', 'Generated automation')}' created in Home Assistant.",
                "automation": automation,
                "home_assistant": inserted.get("response"),
            }

        except Exception as exc:
            logger.error(f"[{self.name}] Failed to create automation: {exc}", exc_info=True)
            return {
                "can_create": False,
                "inserted": False,
                "result": f"Failed to create automation: {exc}",
                "automation": {},
            }


def _normalize_delegate_task_key(task: Any) -> str:
    if isinstance(task, str):
        return task
    try:
        return json.dumps(task, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return str(task)


def _patch_main_actor_delegate_task() -> None:
    try:
        from .main_actor import MainActor
    except Exception:
        return

    if getattr(MainActor, "_ha_delegate_task_patched", False):
        return

    async def _delegate_task_with_normalized_key(self, target_name: str, task: Any, timeout: float = 60.0):
        if not self._registry:
            return None
        target = self._registry.find_by_name(target_name)
        if not target:
            return None

        task_key = _normalize_delegate_task_key(task)
        future = asyncio.get_event_loop().create_future()
        self._result_futures[task_key] = future
        await self.send(
            target.actor_id,
            MessageType.TASK,
            {"text": task, "task": task_key, "reply_to": self.actor_id},
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._result_futures.pop(task_key, None)

    MainActor.delegate_task = _delegate_task_with_normalized_key
    MainActor._ha_delegate_task_patched = True


_patch_main_actor_delegate_task()
