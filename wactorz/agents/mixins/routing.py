"""Intent routing for MainActor.

Classifies each user turn (ACTUATE / HA / PIPELINE / OTHER) with one cheap LLM
call, and handles the one-off Home Assistant actuation path. Mixed into
MainActor; assumes an LLMAgent-derived host (self.llm, self.total_*_tokens,
self._persist_cost) plus the Actor base (self._registry, self.send, self.spawn,
self._result_futures, self.actor_id).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from wactorz.config import CONFIG
from wactorz.llm_factory import provider_for

from ...core.actor import MessageType
from ..prompts.main_actor_prompts import INTENT_CLASSIFIER_PROMPT

if TYPE_CHECKING:
    from .host import RoutingHost

    # Typing-only base: it states what the host must provide, and disappears at
    # runtime so the real MRO is exactly what it was.
    _Host = RoutingHost
else:
    _Host = object

logger = logging.getLogger(__name__)


class RoutingMixin(_Host):
    """Intent classification + one-off actuation. Mix into an LLMAgent host.

    The host contract is `RoutingHost` in `host.py`, declared rather than
    described — this docstring used to list the attributes in prose and nothing
    checked that the list was true.
    """

    async def _classify_intent(self, text: str) -> str:
        """Classify user intent as ACTUATE, HA, PIPELINE, or OTHER using a single cheap LLM call.
        Returns one of: 'ACTUATE', 'HA', 'PIPELINE', 'OTHER'
        """
        if not text or text.startswith("/"):
            return "OTHER"
        llm = provider_for("intent", self.llm)
        if llm is None:
            return "OTHER"
        try:
            decision, _usage = await asyncio.wait_for(
                llm.complete(
                    messages=[{"role": "user", "content": text}],
                    system=INTENT_CLASSIFIER_PROMPT,
                    max_tokens=10,
                    reasoning_effort="none",
                ),
                timeout=60.0,
            )
            self.total_input_tokens += _usage.get("input_tokens", 0)
            self.total_output_tokens += _usage.get("output_tokens", 0)
            self.total_cost_usd += _usage.get("cost_usd", 0.0)
            self._persist_cost()
            token = (decision or "").strip().upper().split()[0] if decision else "OTHER"
            if token in ("HA", "PIPELINE", "OTHER", "ACTUATE"):
                return token
            return "OTHER"
        except asyncio.TimeoutError:
            logger.warning(f"[{self.name}] Intent classification timed out after 60s")
            return "OTHER"
        except Exception as e:
            logger.debug(f"[{self.name}] Intent classification failed: {e}")
            return "OTHER"

    async def _handle_actuate_intent(
        self, text: str, allowed_domains: frozenset[str] | set[str] | None = None
    ) -> str:
        """Resolve and execute a device-control request.

        ``allowed_domains`` restricts which Home Assistant domains may be
        actuated; None (the default, used by the dashboard and CLI) allows all.
        Untrusted callers pass ``SOCIAL_ACTUATE_DOMAINS``.
        """
        if not CONFIG.ha_url or not CONFIG.ha_token:
            return (
                "Home Assistant is not configured. Set `HA_URL` and `HA_TOKEN` in your .env file."
            )

        from ..one_off_actuator_agent import OneOffActuatorAgent

        # ── Enrich the request with HA entity context ──────────────────────
        # The OneOffActuatorAgent needs to resolve "lamp" → "light.wiz_rgbw_tunable_02cba0".
        # Without entity context, it fails with "couldn't identify a matching device".
        # Fetch entities via the home-assistant-agent (cached + fast) and inject
        # the relevant matches into the request so the LLM can pick the right one.
        enriched_text = text
        try:
            if self._registry:
                ha_agent = self._registry.find_by_name("home-assistant-agent")
                if ha_agent:
                    # Use a unique task_id so the future resolves correctly
                    _ha_task_id = f"actuate_entities_{uuid.uuid4().hex[:8]}"
                    _ha_future: asyncio.Future = asyncio.get_running_loop().create_future()
                    self._result_futures[_ha_task_id] = _ha_future
                    await self.send(
                        ha_agent.actor_id,
                        MessageType.TASK,
                        {
                            "text": "list_entities",
                            "_task_id": _ha_task_id,
                            "task": _ha_task_id,
                            "reply_to": self.actor_id,
                        },
                    )
                    try:
                        ha_result = await asyncio.wait_for(_ha_future, timeout=10.0)
                    except asyncio.TimeoutError:
                        ha_result = None
                    finally:
                        self._result_futures.pop(_ha_task_id, None)

                    entities = []
                    if ha_result and isinstance(ha_result, dict):
                        entities = ha_result.get("entities", []) or ha_result.get("result", [])
                    if isinstance(entities, list) and entities:
                        # Build a compact entity summary for the LLM
                        entity_lines = []
                        for e in entities[:300]:
                            eid = e.get("entity_id", "")
                            name = e.get("name", "") or e.get("friendly_name", "")
                            if eid:
                                entry = eid
                                if name and name != eid:
                                    entry += f" ({name})"
                                entity_lines.append(entry)
                        if entity_lines:
                            enriched_text = (
                                f"{text}\n\n"
                                f"[AVAILABLE HA ENTITIES — match the user's device to one of these:\n"
                                + "\n".join(f"  {e}" for e in entity_lines)
                                + "\n]"
                            )
                            logger.info(
                                f"[{self.name}] Enriched actuate request with "
                                f"{len(entity_lines)} HA entities"
                            )
        except Exception as e:
            logger.warning(f"[{self.name}] Could not fetch HA entities for actuate: {e}")

        task_id = f"actuate_{uuid.uuid4().hex[:8]}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._result_futures[task_id] = future

        try:
            await self.spawn(
                OneOffActuatorAgent,
                request=enriched_text,
                llm_provider=provider_for("actuator", self.llm),
                task_id=task_id,
                reply_to_id=self.actor_id,
                allowed_domains=allowed_domains,
                persistence_dir=str(self._persistence_dir.parent),
            )
            result = await asyncio.wait_for(future, timeout=120.0)
            return result.get("result", "Done.")
        except asyncio.TimeoutError:
            return "Actuation timed out, please retry."
        finally:
            self._result_futures.pop(task_id, None)

    async def _is_home_automation_request(self, text: str) -> bool:
        # Keep for backward compat — delegates to _classify_intent
        intent = await self._classify_intent(text)
        return intent == "HA"
