"""LLMAgent - An actor backed by a Large Language Model.
Supports Anthropic Claude, OpenAI, Ollama (local), and custom providers.
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from ..core.actor import Actor, Message, MessageType
from ..core.persistence import get_db
from .llm.base import LLMProvider, ToolCall, ToolCompletion
from .llm.cost import (
    accumulate_global_cost,
    check_cost_limit,
    get_global_alltime_cost,
    get_global_cost_info,
    reset_global_cost,
    set_cost_limit,
)
from .llm.pricing import refresh_pricing
from .llm.providers.anthropic import AnthropicProvider
from .llm.providers.gemini import GeminiProvider
from .llm.providers.nim import NIMProvider
from .llm.providers.ollama import OllamaProvider
from .llm.providers.openai import OpenAIProvider
from .llm.timectx import current_time_context

__all__ = [
    "AnthropicProvider",
    "GeminiProvider",
    "LLMAgent",
    "LLMProvider",
    "NIMProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "ToolCall",
    "ToolCompletion",
    "get_global_alltime_cost",
    "get_global_cost_info",
    "reset_global_cost",
    "set_cost_limit",
]


logger = logging.getLogger(__name__)


class LLMAgent(Actor):
    """An Actor that uses an LLM to process tasks.
    Maintains conversation history and supports tool use.
    """

    def __init__(
        self,
        llm_provider: LLMProvider | None = None,
        system_prompt: str = "You are a helpful AI agent.",
        max_history: int = 20,
        summarize_threshold: int = 30,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.llm = llm_provider
        self.system_prompt = system_prompt
        self.max_history = max_history
        self.summarize_threshold = summarize_threshold  # compress when history exceeds this
        self._conversation_history: list[dict] = []
        self._history_summary: str = ""  # rolling summary of compressed messages
        self._current_task = "idle"
        # Cost / token tracking — must be set here so subclasses (MainActor etc.) inherit them
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost_usd = 0.0
        self._last_persisted_usd = 0.0

    def _current_task_description(self) -> str:
        return self._current_task

    def _preferred_timezone_name(self) -> str | None:
        """Override hook for subclasses that know the user's timezone.

        Base LLMAgent has no access to user facts, so it returns None and
        resolution falls through to the WACTORZ_TZ / TZ env vars and finally the
        system-local zone. MainActor overrides this to return the persisted
        ``pref_timezone`` fact.
        """
        return None

    def _now_context(self) -> str:
        """Live date/time block for this agent, honoring its preferred tz."""
        return current_time_context(self._preferred_timezone_name())

    def _system_prompt_with_now(self) -> str:
        """System prompt with the live date/time block prepended every turn."""
        return self._now_context() + "\n" + self.system_prompt

    async def on_start(self):
        _ = asyncio.create_task(refresh_pricing())
        # Restore conversation history and rolling summary from persistence
        saved = self.recall("conversation_history", [])
        clean = []
        for m in saved:
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            content = m.get("content", "")
            if role not in ("user", "assistant"):
                continue
            if not isinstance(content, str):
                content = str(content)
            if content.strip():
                entry: dict = {"role": role, "content": content}
                if "ts" in m and isinstance(m["ts"], (int, float)):
                    entry["ts"] = m["ts"]
                clean.append(entry)
        self._conversation_history = clean[-self.max_history :]
        self._history_summary = self.recall("history_summary", "")

        # Restore lifetime cost so heartbeats carry accurate totals after restart.
        # Only into a fresh instance: the supervisor restarts by building a new
        # agent, but the start command restarts this same object, whose totals
        # are already the live ones — adding the persisted values there doubles
        # the agent's lifetime spend, and that figure reaches both the dashboard
        # headline and the durable ledger.
        #
        # All three are checked, not just the cost: a locally hosted model with
        # no pricing leaves cost at zero while tokens accumulate, and those would
        # double on their own.
        if not (self.total_input_tokens or self.total_output_tokens or self.total_cost_usd):
            saved_cost = self.recall("_final_cost", {})
            if isinstance(saved_cost, dict):
                self.total_input_tokens += saved_cost.get("input_tokens", 0)
                self.total_output_tokens += saved_cost.get("output_tokens", 0)
                self.total_cost_usd += saved_cost.get("cost_usd", 0.0)
        # Align persisted baseline so global cost doesn't re-add lifetime spend on first
        # _persist_cost() after restart.
        self._last_persisted_usd = self.total_cost_usd

        # Migration: if _messages_processed key doesn't exist yet, seed from
        # conversation_history so the overview counter isn't always 0 on first
        # start after upgrading. messages_processed was set from SQLite in
        # actor.start() — if it's still 0 here, the key was absent.
        if self.metrics.messages_processed == 0 and self._conversation_history:
            user_turns = sum(1 for m in self._conversation_history if m.get("role") == "user")
            self.metrics.messages_processed = user_turns

        # Publish capability manifest so main's topic registry knows this agent exists
        description = (
            getattr(self, "DESCRIPTION", None)
            or (self.__class__.__doc__ or "").strip().split("\n")[0]
            or self.name
        )
        capabilities = getattr(self, "CAPABILITIES", [])
        input_schema = getattr(self, "INPUT_SCHEMA", {})
        output_schema = getattr(self, "OUTPUT_SCHEMA", {})
        await self.publish_manifest(
            description=description,
            capabilities=capabilities,
            input_schema=input_schema,
            output_schema=output_schema,
        )

    async def on_stop(self):
        self.persist("conversation_history", self._conversation_history)
        self.persist("history_summary", self._history_summary)

    async def _maybe_summarize(self):
        """If history exceeds summarize_threshold, compress the oldest half into a
        rolling summary and keep only the most recent max_history messages.
        The summary is prepended as a system-style context message when sending
        to the LLM so no facts are lost.
        """
        if len(self._conversation_history) < self.summarize_threshold:
            return
        if self.llm is None:
            # No LLM — just truncate
            self._conversation_history = self._conversation_history[-self.max_history :]
            return

        # Split: compress the older half, keep the recent half
        split = len(self._conversation_history) // 2
        to_compress = self._conversation_history[:split]
        to_keep = self._conversation_history[split:]

        # Build compression prompt
        prior_summary = (
            f"Previous summary:\n{self._history_summary}\n\n" if self._history_summary else ""
        )
        messages_text = "\n".join(f"{m['role'].upper()}: {m['content'][:400]}" for m in to_compress)
        prompt = (
            f"{prior_summary}"
            f"Summarize the following conversation segment concisely. "
            f"Preserve: key facts, decisions, user preferences, entity names, URLs, credentials, "
            f"any technical details mentioned. Be specific, not vague.\n\n"
            f"{messages_text}"
        )
        try:
            summary, usage = await self.llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system="You are a conversation summarizer. Output a dense, factual summary. No preamble.",
                max_tokens=400,
            )
            self.total_input_tokens += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_cost_usd += usage.get("cost_usd", 0.0)
            self._persist_cost()
            self._history_summary = summary.strip()
            self._conversation_history = to_keep
            self.persist("history_summary", self._history_summary)
            self.persist("conversation_history", self._conversation_history)
            logger.info(
                f"[{self.name}] History summarized: {len(to_compress)} messages → summary ({len(summary)} chars), keeping {len(to_keep)}"
            )
        except Exception as e:
            logger.warning(f"[{self.name}] Summarization failed: {e} — truncating instead")
            self._conversation_history = self._conversation_history[-self.max_history :]

    def _build_messages_with_summary(self, n: int) -> list[dict]:
        """Build the message list to send to the LLM, prepending the rolling summary
        as context if one exists.
        """
        recent = self._conversation_history[-n:]
        if not self._history_summary:
            return recent
        # Inject summary as a user/assistant exchange so it fits the messages format
        summary_ctx = [
            {
                "role": "user",
                "content": f"[Context from earlier in our conversation]\n{self._history_summary}",
            },
            {"role": "assistant", "content": "Understood, I have that context."},
        ]
        return summary_ctx + recent

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            await self._handle_task(msg)

    async def _reply_to_task(self, msg: Message, result: dict) -> None:
        """Answer a task's sender, echoing `_task_id` so their future resolves.

        Every exit from `_handle_task` goes through here, including the failures.
        A path that returns without replying leaves the caller blocked until its
        own timeout, and the reason is visible only in this side's log — so the
        caller cannot tell a slow task from a dead one.

        Senders that supplied no reply address get nothing, which keeps
        fire-and-forget tasks from gaining a phantom reply.
        """
        payload_dict = msg.payload if isinstance(msg.payload, dict) else {}
        reply_to = payload_dict.get("_reply_to") or msg.reply_to or msg.sender_id
        if not reply_to:
            return
        task_id = payload_dict.get("_task_id")
        if task_id:
            result = {**result, "_task_id": task_id}
        await self.send(reply_to, MessageType.RESULT, result)

    async def _handle_task(self, msg: Message):
        if isinstance(msg.payload, dict):
            # Accept "text", "task", "message", or fall back to JSON dump
            task_text = (
                msg.payload.get("text")
                or msg.payload.get("task")
                or msg.payload.get("message")
                or msg.payload.get("query")
                or str(msg.payload)
            )
        else:
            task_text = str(msg.payload) if msg.payload is not None else ""
        self._current_task = task_text[:60]

        if self.llm is None:
            logger.warning(f"[{self.name}] No LLM provider configured.")
            await self._reply_to_task(msg, {"text": "[No LLM configured]", "task": task_text})
            return

        try:
            check_cost_limit()
        except RuntimeError as e:
            await self._reply_to_task(msg, {"text": str(e), "task": task_text})
            return

        start = time.time()
        try:
            self._conversation_history.append({"role": "user", "content": task_text, "ts": start})

            safe_history = [
                {"role": m["role"], "content": str(m["content"])}
                for m in self._conversation_history[-self.max_history :]
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            ]
            response, usage = await self.llm.complete(
                messages=safe_history,
                system=self._system_prompt_with_now(),
            )

            self._conversation_history.append(
                {"role": "assistant", "content": response, "ts": time.time()}
            )
            self.metrics.tasks_completed += 1
            duration = time.time() - start

            self.total_input_tokens += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_cost_usd += usage.get("cost_usd", 0.0)

            # Persist after each exchange
            self.persist("conversation_history", self._conversation_history)
            self._persist_cost()

            # Publish completion
            await self._mqtt_publish(
                f"agents/{self.actor_id}/completed",
                {
                    "result_preview": response[:200],
                    "duration": duration,
                    "task": task_text[:60],
                },
            )

            await self._reply_to_task(
                msg, {"text": response, "task": task_text, "duration": duration}
            )

        except Exception as e:
            self.metrics.tasks_failed += 1
            self.state_value = "failed_task"
            logger.error(f"[{self.name}] LLM task failed: {e}", exc_info=True)
            # The caller is waiting on a future; tell it the turn is over rather
            # than leaving it to time out with no idea what happened.
            await self._reply_to_task(msg, {"text": f"[error] {e}", "task": task_text})

        finally:
            self._current_task = "idle"

    async def chat(self, user_message: str) -> str:
        """Direct async call - useful for the main conversation actor."""
        if self.llm is None:
            return "[No LLM configured]"
        try:
            check_cost_limit()
        except RuntimeError as e:
            return str(e)

        self.metrics.messages_processed += 1
        ts_user = time.time()
        self._conversation_history.append({"role": "user", "content": user_message, "ts": ts_user})

        safe_history = [
            {"role": m["role"], "content": str(m["content"])}
            for m in self._build_messages_with_summary(self.max_history)
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and m.get("content") is not None
        ]
        response, usage = await self.llm.complete(
            messages=safe_history,
            system=self._system_prompt_with_now(),
        )
        ts_reply = time.time()
        self._conversation_history.append(
            {"role": "assistant", "content": response, "ts": ts_reply}
        )
        await self._maybe_summarize()
        self.persist("conversation_history", self._conversation_history)
        self._log_chat_turn(user_message, response, ts_user=ts_user, ts_reply=ts_reply)

        # Accumulate token usage and cost
        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd += usage.get("cost_usd", 0.0)
        self._persist_cost()

        await self._mqtt_publish(
            f"agents/{self.actor_id}/metrics",
            self._build_metrics(),
        )
        return response

    async def chat_stream(self, user_message: str) -> AsyncGenerator[str | dict[str, Any], None]:
        """Streaming version of chat(). Yields text chunks, then a final usage dict.
        The caller is responsible for printing chunks as they arrive.

        Usage:
            async for chunk in agent.chat_stream("hello"):
                if isinstance(chunk, dict):
                    usage = chunk   # final usage summary
                else:
                    print(chunk, end="", flush=True)
        """
        try:
            check_cost_limit()
        except RuntimeError as e:
            yield str(e)
            return

        # Not hasattr: `stream` is defined on the base, so every provider has
        # one. What varies is whether the provider implemented it.
        if self.llm is None or not self.llm.supports_streaming():
            # Fallback: non-streaming — yield whole response as single chunk
            response = await self.chat(user_message)
            yield response
            return

        self.metrics.messages_processed += 1
        self._conversation_history.append(
            {"role": "user", "content": user_message, "ts": time.time()}
        )

        full_text = []
        usage = {}

        safe_history = [
            {"role": m["role"], "content": str(m["content"])}
            for m in self._build_messages_with_summary(self.max_history)
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and m.get("content") is not None
        ]
        try:
            async for chunk in self.llm.stream(
                messages=safe_history,
                system=self._system_prompt_with_now(),
            ):
                if isinstance(chunk, dict):
                    usage = chunk
                else:
                    full_text.append(chunk)
                    yield chunk
        except BaseException:
            # Interrupted mid-stream (Ctrl+C, task cancellation, network error).
            # Persist whatever chunks arrived so neither the user message nor
            # the partial response are lost on restart.
            if full_text:
                partial = "".join(full_text)
                self._conversation_history.append({"role": "assistant", "content": partial})
                self.persist("conversation_history", self._conversation_history)
            raise

        response = "".join(full_text)
        if usage.get("error"):
            # The reply is kept — the tokens are real — but this is the only
            # place the truncation is visible, so it must not pass in silence.
            logger.warning(
                "[%s] streamed reply is incomplete after %d chars: %s",
                self.name,
                len(response),
                usage.get("error", ""),
            )
        self._conversation_history.append(
            {"role": "assistant", "content": response, "ts": time.time()}
        )
        await self._maybe_summarize()
        self.persist("conversation_history", self._conversation_history)

        self.total_input_tokens += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd += usage.get("cost_usd", 0.0)
        self._persist_cost()

        await self._mqtt_publish(
            f"agents/{self.actor_id}/metrics",
            self._build_metrics(),
        )

        # Yield final usage dict so caller can log it
        yield usage

    def _log_chat_turn(self, user_msg: str, reply: str, ts_user: float, ts_reply: float) -> None:
        """Write both halves of a turn to SQLite chat_log and InfluxDB (if enabled)."""
        db = get_db()
        if db is not None:
            try:
                db.log_chat(self.name, "user", user_msg, ts=ts_user, session_id=self.actor_id)
                db.log_chat(self.name, "assistant", reply, ts=ts_reply, session_id=self.actor_id)
            except Exception as exc:
                logger.debug("[%s] chat_log SQLite write failed: %s", self.name, exc)
        try:
            from ..monitoring.influx import write_chat as _influx_chat

            _influx_chat(self.name, "user", user_msg, ts=ts_user)
            _influx_chat(self.name, "assistant", reply, ts=ts_reply)
        except Exception as exc:
            logger.debug("[%s] chat_log InfluxDB write failed: %s", self.name, exc)

    def _persist_cost(self):
        """Write lifetime cost to durable SQLite storage after each exchange."""
        delta = self.total_cost_usd - self._last_persisted_usd
        if delta > 0:
            accumulate_global_cost(delta)
            self._last_persisted_usd = self.total_cost_usd
        self.persist(
            "_final_cost",
            {
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
                "cost_usd": round(self.total_cost_usd, 6),
                "name": self.name,
            },
        )

    def _build_metrics(self) -> dict:
        m = super()._build_metrics()
        m["input_tokens"] = self.total_input_tokens
        m["output_tokens"] = self.total_output_tokens
        m["cost_usd"] = round(self.total_cost_usd, 6)
        return m

    def clear_history(self):
        self._conversation_history = []
