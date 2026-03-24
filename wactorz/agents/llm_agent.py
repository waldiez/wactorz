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
    # NVIDIA NIM — free tier: 1000 req/month per model
    # Most models are free; paid ones listed below
    "nvidia/llama-3.1-nemotron-70b": ( 0.35,  0.40),
    "meta/llama-3.1-405b":           ( 3.45,  3.45),
    "mistralai/mistral-large":       ( 2.00,  6.00),
    # All other NIM models: $0 (free tier)
    "nim/":                          ( 0.00,  0.00),
    # Google Gemini — prices per 1M tokens (input, output), standard context ≤200K tokens
    # Updated March 2026 — https://ai.google.dev/gemini-api/docs/pricing
    "gemini-2.5-flash-lite":         ( 0.10,  0.40),
    "gemini-2.0-flash":              ( 0.10,  0.40),
    "gemini-2.5-flash":              ( 0.30,  2.50),
    "gemini-3-flash":                ( 0.50,  3.00),
    "gemini-2.5-pro":                ( 1.25, 10.00),
    "gemini-3.1-pro":                ( 2.00, 12.00),
    # All other gemini models: approximate flash pricing
    "gemini-":                       ( 0.30,  2.50),
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

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        input_tokens = output_tokens = 0
        async with self.client.messages.stream(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", 4096),
            system=system,
            messages=messages,
        ) as s:
            async for chunk in s.text_stream:
                yield chunk
            # Final message has usage counts
            final = await s.get_final_message()
            input_tokens  = final.usage.input_tokens
            output_tokens = final.usage.output_tokens
        yield {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model, input_tokens, output_tokens),
        }


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
            max_completion_tokens=kwargs.get("max_tokens", 4096),
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

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        input_tokens = output_tokens = 0
        async with await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            max_completion_tokens=kwargs.get("max_tokens", 4096),
            stream=True,
            stream_options={"include_usage": True},
        ) as s:
            async for chunk in s:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
                if chunk.usage:
                    input_tokens  = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
        yield {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model, input_tokens, output_tokens),
        }


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

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        import aiohttp, json as _json
        payload = {"model": self.model, "messages": messages, "stream": True}
        if system:
            payload["system"] = system
        input_tokens = output_tokens = 0
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.base_url}/api/chat", json=payload) as resp:
                async for raw in resp.content:
                    if not raw.strip():
                        continue
                    try:
                        data = _json.loads(raw)
                    except Exception:
                        continue
                    delta = (data.get("message") or {}).get("content", "")
                    if delta:
                        yield delta
                    if data.get("done"):
                        input_tokens  = data.get("prompt_eval_count", 0)
                        output_tokens = data.get("eval_count", 0)
        yield {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": 0.0}


class NIMProvider(LLMProvider):
    """
    NVIDIA NIM — OpenAI-compatible API hosted at integrate.api.nvidia.com.
    Free tier: 1000 requests/month per model. No local GPU required.

    Popular free models:
      meta/llama-3.1-8b-instruct          — fast, lightweight
      meta/llama-3.3-70b-instruct         — strong general purpose
      mistralai/mistral-7b-instruct-v0.3  — fast & capable
      mistralai/mixtral-8x7b-instruct-v0.1
      google/gemma-3-27b-it
      microsoft/phi-3-mini-128k-instruct
      deepseek-ai/deepseek-r1             — reasoning model
      deepseek-ai/deepseek-r1-distill-qwen-7b
      nvidia/llama-3.1-nemotron-70b-instruct
      nvidia/llama-3.3-nemotron-super-49b-v1

    Get a free API key at: https://build.nvidia.com
    """

    NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"

    def __init__(
        self,
        model:    str = "meta/llama-3.3-70b-instruct",
        api_key:  Optional[str] = None,
        base_url: str = NIM_BASE_URL,
    ):
        import openai
        self.model  = model
        self.client = openai.AsyncOpenAI(
            api_key=api_key or "dummy",   # NIM free tier may not require a key locally
            base_url=base_url,
        )

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            max_tokens=kwargs.get("max_tokens", 4096),
        )
        text = response.choices[0].message.content
        input_tok  = response.usage.prompt_tokens     if response.usage else 0
        output_tok = response.usage.completion_tokens if response.usage else 0
        usage = {
            "input_tokens":  input_tok,
            "output_tokens": output_tok,
            "cost_usd":      _calc_cost(self.model, input_tok, output_tok),
        }
        return text, usage

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        full_messages = ([{"role": "system", "content": system}] if system else []) + messages
        input_tokens = output_tokens = 0
        async with await self.client.chat.completions.create(
            model=self.model,
            messages=full_messages,
            max_tokens=kwargs.get("max_tokens", 4096),
            stream=True,
        ) as s:
            async for chunk in s:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
                if chunk.usage:
                    input_tokens  = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens
        yield {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model, input_tokens, output_tokens),
        }


class GeminiProvider(LLMProvider):
    """
    Google Gemini via the official google-generativeai SDK.
    Install: pip install google-generativeai

    Recommended models (March 2026):
      gemini-2.5-flash-lite   — cheapest ($0.10/$0.40 per 1M tokens), fast, free tier
      gemini-2.0-flash        — fast & capable ($0.10/$0.40), free tier available
      gemini-2.5-flash        — hybrid reasoning ($0.30/$2.50), free tier available
      gemini-2.5-pro          — best for coding & complex tasks ($1.25/$10.00)
      gemini-3.1-pro          — flagship ($2.00/$12.00), no free tier

    Get a free API key at: https://aistudio.google.com
    Note: Pro models charge 2x for prompts >200K tokens.
    """

    def __init__(
        self,
        model:   str = "gemini-2.5-flash",
        api_key: Optional[str] = None,
    ):
        import google.generativeai as genai
        if api_key:
            genai.configure(api_key=api_key)
        self.model_name = model
        self._genai = genai

    def _get_model(self):
        return self._genai.GenerativeModel(self.model_name)

    async def complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        import asyncio
        model = self._get_model()

        # Convert messages to Gemini format
        # System prompt goes into system_instruction, history is contents
        if system:
            model = self._genai.GenerativeModel(
                self.model_name,
                system_instruction=system,
            )

        contents = self._to_gemini_contents(messages)

        # Run in executor since the SDK is sync
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(contents),
        )

        text = response.text or ""
        input_tokens  = response.usage_metadata.prompt_token_count     if response.usage_metadata else 0
        output_tokens = response.usage_metadata.candidates_token_count if response.usage_metadata else 0

        usage = {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model_name, input_tokens, output_tokens),
        }
        return text, usage

    async def stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        import asyncio
        import queue as _queue

        model = self._genai.GenerativeModel(self.model_name)
        if system:
            model = self._genai.GenerativeModel(
                self.model_name,
                system_instruction=system,
            )

        contents = self._to_gemini_contents(messages)

        # Stream via SDK in a thread, bridge to async via queue
        q: _queue.Queue = _queue.Queue()
        input_tokens = output_tokens = 0

        def _stream_thread():
            try:
                for chunk in model.generate_content(contents, stream=True):
                    if chunk.text:
                        q.put(("text", chunk.text))
                    if chunk.usage_metadata:
                        q.put(("usage", chunk.usage_metadata))
            except Exception as e:
                q.put(("error", str(e)))
            finally:
                q.put(("done", None))

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _stream_thread)

        while True:
            try:
                kind, value = await loop.run_in_executor(None, lambda: q.get(timeout=60))
            except Exception:
                break
            if kind == "done":
                break
            elif kind == "text":
                yield value
            elif kind == "usage":
                input_tokens  = value.prompt_token_count     or 0
                output_tokens = value.candidates_token_count or 0
            elif kind == "error":
                logger.error(f"[GeminiProvider] Stream error: {value}")
                break

        yield {
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "cost_usd":      _calc_cost(self.model_name, input_tokens, output_tokens),
        }

    @staticmethod
    def _to_gemini_contents(messages: list[dict]) -> list[dict]:
        """Convert OpenAI-style messages to Gemini contents format."""
        contents = []
        for m in messages:
            role = m.get("role", "user")
            content = str(m.get("content", ""))
            # Gemini uses "user" and "model" (not "assistant")
            gemini_role = "model" if role == "assistant" else "user"
            # Merge consecutive same-role messages (Gemini requires alternating)
            if contents and contents[-1]["role"] == gemini_role:
                contents[-1]["parts"][0]["text"] += "\n" + content
            else:
                contents.append({"role": gemini_role, "parts": [{"text": content}]})
        return contents


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
        summarize_threshold: int = 30,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.llm = llm_provider
        self.system_prompt = system_prompt
        self.max_history = max_history
        self.summarize_threshold = summarize_threshold  # compress when history exceeds this
        self._conversation_history: list[dict] = []
        self._history_summary: str = ""   # rolling summary of compressed messages
        self._current_task = "idle"
        # Cost / token tracking — must be set here so subclasses (MainActor etc.) inherit them
        self.total_input_tokens  = 0
        self.total_output_tokens = 0
        self.total_cost_usd      = 0.0

    def _current_task_description(self) -> str:
        return self._current_task

    async def on_start(self):
        # Restore conversation history and rolling summary from persistence
        saved = self.recall("conversation_history", [])
        clean = []
        for m in saved:
            if not isinstance(m, dict):
                continue
            role    = m.get("role", "")
            content = m.get("content", "")
            if role not in ("user", "assistant"):
                continue
            if not isinstance(content, str):
                content = str(content)
            if content.strip():
                clean.append({"role": role, "content": content})
        self._conversation_history = clean[-self.max_history:]
        self._history_summary = self.recall("history_summary", "")

        # Publish capability manifest so main's topic registry knows this agent exists
        description = (
            getattr(self, "DESCRIPTION", None)
            or (self.__class__.__doc__ or "").strip().split("\n")[0]
            or self.name
        )
        capabilities  = getattr(self, "CAPABILITIES", [])
        input_schema  = getattr(self, "INPUT_SCHEMA",  {})
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
        """
        If history exceeds summarize_threshold, compress the oldest half into a
        rolling summary and keep only the most recent max_history messages.
        The summary is prepended as a system-style context message when sending
        to the LLM so no facts are lost.
        """
        if len(self._conversation_history) < self.summarize_threshold:
            return
        if self.llm is None:
            # No LLM — just truncate
            self._conversation_history = self._conversation_history[-self.max_history:]
            return

        # Split: compress the older half, keep the recent half
        split = len(self._conversation_history) // 2
        to_compress = self._conversation_history[:split]
        to_keep     = self._conversation_history[split:]

        # Build compression prompt
        prior_summary = f"Previous summary:\n{self._history_summary}\n\n" if self._history_summary else ""
        messages_text = "\n".join(
            f"{m['role'].upper()}: {m['content'][:400]}"
            for m in to_compress
        )
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
            self.total_input_tokens  += usage.get("input_tokens", 0)
            self.total_output_tokens += usage.get("output_tokens", 0)
            self.total_cost_usd      += usage.get("cost_usd", 0.0)
            self._history_summary = summary.strip()
            self._conversation_history = to_keep
            self.persist("history_summary", self._history_summary)
            self.persist("conversation_history", self._conversation_history)
            logger.info(f"[{self.name}] History summarized: {len(to_compress)} messages → summary ({len(summary)} chars), keeping {len(to_keep)}")
        except Exception as e:
            logger.warning(f"[{self.name}] Summarization failed: {e} — truncating instead")
            self._conversation_history = self._conversation_history[-self.max_history:]

    def _build_messages_with_summary(self, n: int) -> list[dict]:
        """
        Build the message list to send to the LLM, prepending the rolling summary
        as context if one exists.
        """
        recent = self._conversation_history[-n:]
        if not self._history_summary:
            return recent
        # Inject summary as a user/assistant exchange so it fits the messages format
        summary_ctx = [{
            "role": "user",
            "content": f"[Context from earlier in our conversation]\n{self._history_summary}"
        }, {
            "role": "assistant",
            "content": "Understood, I have that context."
        }]
        return summary_ctx + recent

    async def handle_message(self, msg: Message):
        if msg.type == MessageType.TASK:
            await self._handle_task(msg)

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
            return

        start = time.time()
        try:
            self._conversation_history.append({"role": "user", "content": task_text})

            response, _usage = await self.llm.complete(
                messages=self._conversation_history[-self.max_history:],
                system=self.system_prompt,
            )

            self._conversation_history.append({"role": "assistant", "content": response})
            self.metrics.tasks_completed += 1
            duration = time.time() - start

            # Persist after each exchange
            self.persist("conversation_history", self._conversation_history)

            # Publish completion
            await self._mqtt_publish(
                f"agents/{self.actor_id}/completed",
                {
                    "result_preview": response[:200],
                    "duration": duration,
                    "task": task_text[:60],
                },
            )

            # Reply to sender — echo _task_id so send_to() futures resolve
            payload_dict = msg.payload if isinstance(msg.payload, dict) else {}
            task_id  = payload_dict.get("_task_id")
            reply_to = payload_dict.get("_reply_to") or msg.reply_to or msg.sender_id
            if reply_to:
                result = {"text": response, "task": task_text, "duration": duration}
                if task_id:
                    result["_task_id"] = task_id
                await self.send(reply_to, MessageType.RESULT, result)

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

        safe_history = [
            {"role": m["role"], "content": str(m["content"])}
            for m in self._build_messages_with_summary(self.max_history)
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and m.get("content") is not None
        ]
        response, usage = await self.llm.complete(
            messages=safe_history,
            system=self.system_prompt,
        )
        self._conversation_history.append({"role": "assistant", "content": response})
        await self._maybe_summarize()
        self.persist("conversation_history", self._conversation_history)

        # Accumulate token usage and cost
        self.total_input_tokens  += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd      += usage.get("cost_usd", 0.0)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/metrics",
            self._build_metrics(),
        )
        return response

    async def chat_stream(self, user_message: str):
        """
        Streaming version of chat(). Yields text chunks, then a final usage dict.
        The caller is responsible for printing chunks as they arrive.

        Usage:
            async for chunk in agent.chat_stream("hello"):
                if isinstance(chunk, dict):
                    usage = chunk   # final usage summary
                else:
                    print(chunk, end="", flush=True)
        """
        if self.llm is None or not hasattr(self.llm, "stream"):
            # Fallback: non-streaming — yield whole response as single chunk
            response = await self.chat(user_message)
            yield response
            return

        self.metrics.messages_processed += 1
        self._conversation_history.append({"role": "user", "content": user_message})

        full_text = []
        usage     = {}

        safe_history = [
            {"role": m["role"], "content": str(m["content"])}
            for m in self._build_messages_with_summary(self.max_history)
            if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and m.get("content") is not None
        ]
        async for chunk in self.llm.stream(
            messages=safe_history,
            system=self.system_prompt,
        ):
            if isinstance(chunk, dict):
                usage = chunk
            else:
                full_text.append(chunk)
                yield chunk

        response = "".join(full_text)
        self._conversation_history.append({"role": "assistant", "content": response})
        await self._maybe_summarize()
        self.persist("conversation_history", self._conversation_history)

        self.total_input_tokens  += usage.get("input_tokens", 0)
        self.total_output_tokens += usage.get("output_tokens", 0)
        self.total_cost_usd      += usage.get("cost_usd", 0.0)

        await self._mqtt_publish(
            f"agents/{self.actor_id}/metrics",
            self._build_metrics(),
        )

        # Yield final usage dict so caller can log it
        yield usage

    def _build_metrics(self) -> dict:
        m = super()._build_metrics()
        m["input_tokens"]  = self.total_input_tokens
        m["output_tokens"] = self.total_output_tokens
        m["cost_usd"]      = round(self.total_cost_usd, 6)
        return m

    def clear_history(self):
        self._conversation_history = []