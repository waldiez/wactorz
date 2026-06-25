"""Memory and system-prompt construction for MainActor.

Holds user-fact persistence/extraction and the dynamic system-prompt rebuild
that injects the live running-agent list and known facts on every turn.
Mixed into MainActor; assumes an LLMAgent-derived host (uses self.llm,
self.total_*_tokens, self._persist_cost, self.system_prompt) plus the Actor
base (self.persist, self.recall, self._registry).
"""

from __future__ import annotations

import logging
from typing import Optional

from ..prompts.main_actor_prompts import ORCHESTRATOR_PROMPT, FACTS_EXTRACT_PROMPT

logger = logging.getLogger(__name__)


class MemoryMixin:
    """User facts + live system-prompt assembly. Mix into an LLMAgent host."""

    def get_user_facts(self) -> dict:
        return self.recall("_user_facts") or {}

    def _preferred_timezone_name(self) -> Optional[str]:
        """Main knows the user's timezone from facts — use it so the live
        date/time block matches what the scheduler actually fires against."""
        return self.get_user_facts().get("pref_timezone")

    def _get_running_agents_summary(self) -> str:
        """
        Build a short, authoritative description of currently running agents
        by reading the live registry (same source the planner uses).
        Returns empty string if registry is unavailable or only main is running.
        """
        if not self._registry:
            return ""
        skip = {"main", "monitor", "installer"}
        lines = []
        for actor in self._registry.all_actors():
            if actor.name in skip:
                continue
            # Skip transient planner instances — they're supervisors of their
            # spawned pipeline agents and not user-facing capabilities.
            if actor.name.startswith("planner-"):
                continue
            desc = (
                getattr(actor, "DESCRIPTION", None)
                or getattr(actor, "description", "")
                or (getattr(actor, "system_prompt", "") or "")[:80]
                or type(actor).__name__
            )
            # Single-line summary, trimmed
            desc = " ".join(str(desc).split())[:120]
            lines.append(f"  {actor.name} — {desc}" if desc else f"  {actor.name}")
        if not lines:
            return ""
        return "\n".join(lines)

    def _prefix_with_live_context(self, user_text: str) -> str:
        """
        Wrap the user's message with a `[CURRENT SYSTEM STATE]` block so the LLM
        sees the live agent list INSIDE the user message — not just the system
        prompt.

        Why both? Models weight in-message content more heavily than system prompts
        for "what is true right now" questions. Having the same list in both places
        is belt-and-braces: the system prompt sets the rule ("trust this list"),
        the per-message prefix supplies fresh evidence the rule applies to.

        The prefix is wrapped in clear delimiters so it's visually obvious to the
        model that it's context, not the user's actual question.
        """
        live_names = []
        if self._registry:
            skip = {"main", "monitor", "installer"}
            for actor in self._registry.all_actors():
                if actor.name in skip:
                    continue
                if actor.name.startswith("planner-"):
                    continue
                live_names.append(actor.name)
        live_names.sort()

        if live_names:
            ctx = (
                "[CURRENT SYSTEM STATE — auto-injected, NOT from the user]\n"
                f"Currently running agents (live, just queried): {', '.join(live_names)}\n"
                "If the user asks what agents exist or are running, answer using EXACTLY\n"
                "this list. Do not add agents from your memory of earlier turns.\n"
                "[END SYSTEM STATE]\n\n"
            )
        else:
            ctx = (
                "[CURRENT SYSTEM STATE — auto-injected, NOT from the user]\n"
                "Currently running agents (live, just queried): NONE\n"
                "No user-spawned agents exist right now. If the user asks what's running,\n"
                "say so plainly. Do not invent agents from earlier in the conversation.\n"
                "[END SYSTEM STATE]\n\n"
            )
        return ctx + user_text

    def _rebuild_system_prompt(self):
        """
        Reconstruct the system prompt from ORCHESTRATOR_PROMPT plus dynamic blocks:
          1. Currently running agents (live registry — authoritative, refreshed each call)
          2. Known user facts (persisted)

        This is the single source of truth for the system prompt. It MUST be called
        before every LLM turn so main never answers from a stale view of the world.
        Both blocks are appended in a fixed order so the prompt is deterministic.

        IMPORTANT: ORCHESTRATOR_PROMPT references agent.capabilities() — that's
        documentation aimed at spawned DynamicAgents which have an _AgentAPI.
        Main itself has NO such method. Without an explicit override, the LLM
        reads the documentation, "calls" the function (it can't), and confabulates
        the result based on conversation history. We prepend an OVERRIDE block that
        tells main directly: the running-agents list below IS the result of that
        lookup, do not pretend to call anything.
        """
        # ── Override block for main specifically ──
        override = (
            "== MAIN-SPECIFIC OVERRIDE (read this FIRST) ==\n"
            "You are 'main'. You are NOT a DynamicAgent. You do NOT have an `agent` object.\n"
            "You CANNOT call agent.capabilities(), agent.send_to(), agent.topics(), "
            "agent.subscribe(), agent.window(), agent.mqtt_get(), or any other agent.* method.\n"
            "Those methods exist for OTHER agents you SPAWN. They do not exist for you.\n\n"
            "If you see those methods mentioned later in this prompt, that is reference\n"
            "documentation for code you WRITE inside <spawn> blocks — NOT a tool you can\n"
            "invoke yourself. Never write 'Let me call agent.capabilities()' in a reply.\n"
            "Never fabricate the output of such a call.\n\n"
            "When the user asks what agents exist, what's running, or anything about\n"
            "current system state, READ THE 'CURRENTLY RUNNING AGENTS' BLOCK BELOW.\n"
            "That block IS your capability lookup — already done for you, refreshed live\n"
            "on every turn. Do not pretend to perform a separate lookup.\n"
        )

        prompt = override + "\n" + ORCHESTRATOR_PROMPT

        # ── Block 1: live running agents (so main knows the truth, not its memory) ──
        # Wording is deliberately strong: the LLM tends to trust earlier conversation
        # turns ("I just spawned X") over the system prompt. We need to override that.
        agents_summary = self._get_running_agents_summary()
        header = (
            "\n\n== CURRENTLY RUNNING AGENTS (LIVE GROUND TRUTH — overrides conversation history) ==\n"
            "This block is regenerated from the live registry on EVERY turn. It is the ONLY\n"
            "authoritative source for which agents exist. If something is not on this list,\n"
            "it does NOT exist right now — even if conversation history says you spawned it.\n"
            "Agents can be deleted by the user at any time, and the conversation will not\n"
            "necessarily mention it. ALWAYS trust this list over your memory of past turns.\n\n"
            "When the user asks what agents are running, list EXACTLY these names — no\n"
            "more, no less. Do not invent entries from memory. Do not include agents from\n"
            "earlier turns that aren't here now.\n\n"
            "When the user asks to spawn an agent and that name is NOT on this list:\n"
            "spawn it. Do NOT say 'it already exists' — that claim is based on stale memory.\n"
        )
        if agents_summary:
            prompt += header + agents_summary
        else:
            prompt += header + "  (no user-spawned agents are currently running)"

        # ── Block 2: persisted user facts, grouped by bucket ──
        facts = self.get_user_facts()
        if facts:
            buckets = {
                "pref_":   ("PREFERENCES & IDENTITY", []),
                "device_": ("DEVICES & SETUP",        []),
                "policy_": ("STANDING POLICIES",      []),
                "":        ("OTHER FACTS",            []),   # legacy / unprefixed
            }
            for k, v in facts.items():
                placed = False
                for prefix, (_, items) in buckets.items():
                    if prefix and k.startswith(prefix):
                        items.append(f"  {k[len(prefix):]}: {v}")
                        placed = True
                        break
                if not placed:
                    buckets[""][1].append(f"  {k}: {v}")

            sections = []
            for _, (heading, items) in buckets.items():
                if items:
                    sections.append(f"\n[{heading}]\n" + "\n".join(items))
            if sections:
                prompt += (
                    "\n\n== KNOWN USER FACTS (always keep in mind) =="
                    + "".join(sections)
                    + "\nWhen a POLICY conflicts with a default behavior, follow the policy."
                )

        self.system_prompt = prompt

    def _inject_user_facts_into_prompt(self):
        """Backward-compatible alias — delegates to the unified rebuild."""
        self._rebuild_system_prompt()

    async def _extract_and_save_facts(self, user_message: str, assistant_response: str):
        """
        After each exchange, ask the LLM to extract any new durable facts.

        Observability: this method logs every attempt at INFO level (start),
        success at INFO (with extracted keys), and failures at WARNING. If you
        suspect facts aren't being saved, search the log for
        '[main] Facts extraction'.

        Namespace normalization: the prompt asks for keys prefixed with one of
        pref_/device_/policy_, but LLMs sometimes return raw keys ('user_name'
        instead of 'pref_user_name'). We normalize on save so a stray unprefixed
        key still ends up in a sensible bucket rather than the OTHER catch-all.
        """
        if self.llm is None:
            logger.warning(f"[{self.name}] Facts extraction skipped: no LLM provider")
            return
        if not user_message or not user_message.strip():
            return
        logger.info(f"[{self.name}] Facts extraction running on: {user_message[:80]!r}")
        exchange = f"USER: {user_message[:600]}\nASSISTANT: {assistant_response[:600]}"
        try:
            raw, _usage = await self.llm.complete(
                messages=[{"role": "user", "content": exchange}],
                system=FACTS_EXTRACT_PROMPT,
                max_tokens=300,
            )
            self.total_input_tokens  += _usage.get("input_tokens", 0)
            self.total_output_tokens += _usage.get("output_tokens", 0)
            self.total_cost_usd      += _usage.get("cost_usd", 0.0)
            self._persist_cost()
            import json as _json
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            if not clean:
                logger.warning(f"[{self.name}] Facts extraction returned empty string")
                return
            new_facts = _json.loads(clean)
            if not isinstance(new_facts, dict):
                logger.warning(f"[{self.name}] Facts extraction returned non-dict: {type(new_facts).__name__}")
                return
            if not new_facts:
                logger.info(f"[{self.name}] Facts extraction: nothing durable in this turn")
                return

            # Normalize keys: if the LLM forgot the namespace prefix, infer one
            # from common patterns. This keeps the bucketed display clean.
            normalized = {}
            for k, v in new_facts.items():
                if k.startswith(("pref_", "device_", "policy_")):
                    normalized[k] = v
                    continue
                # Heuristic guesses for common unprefixed keys
                if k.endswith("_url") or k.endswith("_endpoint") or k.endswith("_path"):
                    normalized[f"device_{k}"] = v
                elif k.startswith(("user_", "favorite_", "pref_")) or k in ("name", "age", "location", "language"):
                    normalized[f"pref_{k}"] = v
                elif "policy" in k or "rule" in k or "threshold" in k:
                    normalized[f"policy_{k}"] = v
                else:
                    normalized[f"pref_{k}"] = v   # default bucket for unknowns

            # Merge with existing facts (supersede on key collision — by design)
            facts = self.get_user_facts()
            superseded = [k for k in normalized if k in facts and facts[k] != normalized[k]]
            facts.update(normalized)
            self.persist("_user_facts", facts)
            self._inject_user_facts_into_prompt()
            log_msg = f"[{self.name}] User facts updated: {list(normalized.keys())}"
            if superseded:
                log_msg += f" (superseded: {superseded})"
            logger.info(log_msg)
        except _json.JSONDecodeError as e:
            logger.warning(
                f"[{self.name}] Facts extraction JSON parse failed: {e}. "
                f"Raw response (first 200 chars): {raw[:200]!r}"
            )
        except Exception as e:
            logger.warning(f"[{self.name}] Facts extraction failed: {e!r}")
