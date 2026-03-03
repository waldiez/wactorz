"""
LLMAgent - An actor backed by a Large Language Model.
Supports Anthropic Claude, OpenAI, Ollama (local), and custom providers.
"""

import asyncio
import logging
import time
from typing import Any, Optional

from ..core.actor import Actor, Message, MessageType

logger = logging.getLogger(__name__)


# Pricing per 1M tokens (input, output) in USD — update as needed
PRICING = {
    # Anthropic
    "claude-opus-4-6":        (15.00, 75.00),
    "claude-sonnet-4-6":      ( 3.00, 15.00),
    "claude-haiku-4-5":       ( 0.80,  4.00),
    # OpenAI
    "gpt-4o":                 ( 2.50, 10.00),
    "gpt-4o-mini":            ( 0.15,  0.60),
    "gpt-4-turbo":            (10.00, 30.00),
    # Ollama — local, no cost
    "ollama":                 ( 0.00,  0.00),
}

def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    key = next((k for k in PRICING if model.startswith(k)), None)
    if not key:
        return 0.0
    price_in, price_out = PRICING[key]
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


class LLMProvider:
    """Base class for LLM providers."""

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        """Returns (text, usage) where usage = {input_tokens, output_tokens, cost_usd}"""
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: Optional[str] = None):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 4096),
            system=system,
            messages=messages,
        )
        text = response.content[0].text
        usage = {
            "input_tokens":  response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_usd":      _calc_cost(self.model,
                                        response.usage.input_tokens,
                                        response.usage.output_tokens),
        }
        return text, usage


class OpenAIProvider(LLMProvider):
    def __init__(self, model: str = "gpt-4o", api_key: Optional[str] = None):
        import openai
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.model = model

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            max_tokens=kwargs.get("max_tokens", 4096),
        )
        text = response.choices[0].message.content
        usage = {
            "input_tokens":  response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
            "cost_usd":      _calc_cost(self.model,
                                        response.usage.prompt_tokens,
                                        response.usage.completion_tokens),
        }
        return text, usage


class OllamaProvider(LLMProvider):
    """Local LLM via Ollama."""
    def __init__(self, model: str = "llama3", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        import aiohttp
        payload = {"model": self.model, "messages": messages, "stream": False}
        if system:
            payload["system"] = system
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/api/chat", json=payload) as resp:
                data = await resp.json()
        text = data["message"]["content"]
        prompt_eval = data.get("prompt_eval_count", 0)
        eval_count  = data.get("eval_count", 0)
        usage = {"input_tokens": prompt_eval, "output_tokens": eval_count, "cost_usd": 0.0}
        return text, usage


class LLMAgent(Actor):
    """
    An Actor that uses an LLM to process tasks.
    Maintains conversation history and supports tool use.
    """

    def __init__(
        self,
        llm_provider: Optional[LLMProvider] = None,
        system_prompt: str = "You are a helpful AI agent.",
        max_history: int = 20,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.llm = llm_provider
        self.system_prompt = system_prompt
        self.max_history = max_history
        self._conversation_history: list[dict] = []
        self._current_task = "idle"
        # Cost / token tracking — must be set here so subclasses (MainActor etc.) inherit them
        self.total_input_tokens  = 0
        self.total_output_tokens = 0
        self.total_cost_usd      = 0.0

    def _current_task_description(self) -> str:
        return self._current_task

    async def on_start(self):
        # Restore conversation history from persistence
        saved = self.recall("conversation_history", [])
        self._conversation_history = saved[-self.max_history:]

    async def on_stop(self):
        self.persist("conversation_history", self._conversation_history)

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            await self._handle_task(msg)

    async def _handle_task(self, msg: Message):
        task_text = msg.payload.get("text") if isinstance(msg.payload, dict) else str(msg.payload)
        self._current_task = task_text[:60]

        if self.llm is None:
            logger.warning(f"[{self.name}] No LLM provider configured.")
            return

        start = time.time()
        try:
            self._conversation_history.append({"role": "user", "content": task_text})

            response = await self.llm.complete(
                messages=self._conversation_history[-self.max_history:],
                system=self.system_prompt,
            )

            self._conversation_history.append({"role": "assistant", "content": response})
            self.metrics.tasks_completed += 1
            duration = time.time() - start

            # Persist after each exchange
            self.persist("conversation_history", self._conversation_history)
            await self._save_persistent_state()

            # Publish completion
            await self._mqtt_publish(
                f"agents/{self.actor_id}/completed",
                {
                    "result_preview": response[:200],
                    "duration": duration,
                    "task": task_text[:60],
                },
            )

            # Reply to sender
            if msg.reply_to or msg.sender_id:
                target = msg.reply_to or msg.sender_id
                await self.send(target, MessageType.RESULT, {
                    "text": response,
                    "task": task_text,
                    "duration": duration,
                })

        except Exception as e:
            self.metrics.tasks_failed += 1
            self.state_value = "failed_task"
            logger.error(f"[{self.name}] LLM task failed: {e}", exc_info=True)

        finally:
            self._current_task = "idle"

    async def chat(self, user_message: str) -> str:
        """Direct async call - useful for the main conversation actor."""
        if self.llm is None:
            return "[No LLM configured]"

        self.metrics.messages_processed += 1
        self._conversation_history.append({"role": "user", "content": user_message})
        response, usage = await self.llm.complete(
            messages=self._conversation_history[-self.max_history:],
            system=self.system_prompt,
        )
        self._conversation_history.append({"role": "assistant", "content": response})

        # Accumulate token usage and cost
        self.total_input_tokens  += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd      += usage.get("cost_usd", 0.0)

        # Publish updated metrics immediately so dashboard reflects the new count
        await self._mqtt_publish(
            f"agents/{self.actor_id}/metrics",
            self._build_metrics(),
        )
        return response

    def _build_metrics(self) -> dict:
        m = super()._build_metrics()
        m["input_tokens"]  = self.total_input_tokens
        m["output_tokens"] = self.total_output_tokens
        m["cost_usd"]      = round(self.total_cost_usd, 6)
        return m

    def clear_history(self):
        self._conversation_history = []
