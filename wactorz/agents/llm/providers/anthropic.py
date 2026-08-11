"""AnthropicProvider — Claude, via the anthropic SDK."""

import logging
from typing import Any

from ..base import LLMProvider, ToolCall, ToolCompletion, _temp_params
from ..pricing import calc_cost

logger = logging.getLogger(__name__)

# Anthropic removed the sampling parameters on Claude Opus 4.7 and has kept them
# out of every model released since: `temperature` is a 400, not a field the API
# ignores. Substring rather than exact match, so dated and platform-prefixed ids
# ("anthropic.claude-opus-5", "claude-opus-5-20260401") answer like the alias.
_NO_SAMPLING_PARAMS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-",
    "claude-mythos-",
)

# Models that turned out to reject `temperature` at runtime. The list above
# cannot name a model released after it was written, so the first request on one
# pays a 400 and lands here; every later request in the process skips the
# parameter instead of paying that 400 again.
_learned_no_sampling: set[str] = set()

# Models where OMITTING `thinking` means adaptive thinking, not thinking-off.
# Claude Sonnet 4.6 and Opus 4.8 and earlier run thinking-off when the parameter
# is absent, so a caller asking for no reasoning got it by saying nothing. On the
# models below the same absent parameter means "think as much as you like", and
# the reasoning is billed against `max_tokens` before any text block opens. A
# call site with a tight budget — the intent classifier runs at max_tokens=10 —
# then gets a well-formed response whose only content is a thinking block, and
# `_extract_text` correctly returns "". Nothing errors; the caller just sees an
# empty string. These models must be told explicitly to stop thinking.
_THINKING_ON_BY_DEFAULT: tuple[str, ...] = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-",
    "claude-mythos-",
)

# Claude Fable 5 and Claude Mythos 5 reject an explicit `thinking: {"type":
# "disabled"}` outright, at any effort — there is no way to turn their reasoning
# off, so the only correct request omits the parameter and the only correct call
# site budgets for it.
_CANNOT_DISABLE_THINKING: tuple[str, ...] = ("claude-fable-", "claude-mythos-")

# Same learned-fallback shape as `_learned_no_sampling`: a model released after
# this file may refuse `thinking: {"type": "disabled"}` in a way we cannot name
# here.
_learned_no_thinking_param: set[str] = set()

# Reasoning depth, cheapest first. Anthropic takes it as `output_config.effort`,
# not as part of the `thinking` block.
_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Levels each family accepts. `max` arrived with the 4.6 generation and `xhigh`
# with Opus 4.7, so a level the model has never heard of is a 400 rather than a
# ceiling it quietly applies — a request is clamped down to the highest level
# the model does know. Families absent from this table take no effort parameter
# at all (Sonnet 4.5, Haiku 4.5 and older): they still get adaptive thinking,
# just without a depth hint.
_WITHOUT_XHIGH = frozenset({"low", "medium", "high", "max"})
_EVERY_LEVEL = frozenset(_EFFORT_LEVELS)
_EFFORT_SUPPORT: tuple[tuple[str, frozenset[str]], ...] = (
    ("claude-opus-4-5", frozenset({"low", "medium", "high"})),
    ("claude-opus-4-6", _WITHOUT_XHIGH),
    ("claude-sonnet-4-6", _WITHOUT_XHIGH),
    ("claude-opus-4-7", _EVERY_LEVEL),
    ("claude-opus-4-8", _EVERY_LEVEL),
    ("claude-opus-5", _EVERY_LEVEL),
    ("claude-sonnet-5", _EVERY_LEVEL),
    ("claude-fable-", _EVERY_LEVEL),
    ("claude-mythos-", _EVERY_LEVEL),
)

# Models that rejected `output_config` at runtime, learned the same way.
_learned_no_effort: set[str] = set()

# What the OpenAI-shaped call sites may send that Anthropic has no name for.
_EFFORT_ALIASES: dict[str, str] = {"minimal": "low"}


def _rejects_temperature(model: str) -> bool:
    """Whether this model refuses `temperature` outright."""
    name = model.lower()
    return model in _learned_no_sampling or any(family in name for family in _NO_SAMPLING_PARAMS)


def _thinks_by_default(model: str) -> bool:
    """Whether omitting `thinking` leaves reasoning switched on for this model."""
    name = model.lower()
    return any(family in name for family in _THINKING_ON_BY_DEFAULT)


def _can_disable_thinking(model: str) -> bool:
    """Whether this model accepts an explicit `thinking: {"type": "disabled"}`."""
    name = model.lower()
    if model in _learned_no_thinking_param:
        return False
    return not any(family in name for family in _CANNOT_DISABLE_THINKING)


def _effort_for(model: str, reasoning_effort: str | None) -> str | None:
    """The `output_config.effort` level to send, or None to omit it.

    Clamped to the highest level this model actually accepts, so asking for
    depth a model does not offer costs a level rather than the whole turn.
    """
    if reasoning_effort in (None, "none") or model in _learned_no_effort:
        return None
    wanted = _EFFORT_ALIASES.get(str(reasoning_effort).lower(), str(reasoning_effort).lower())
    if wanted not in _EFFORT_LEVELS:
        return None
    name = model.lower()
    supported = next(
        (levels for family, levels in _EFFORT_SUPPORT if family in name),
        None,
    )
    if not supported:
        return None
    ceiling = _EFFORT_LEVELS.index(wanted)
    for level in reversed(_EFFORT_LEVELS[: ceiling + 1]):
        if level in supported:
            return level
    return None


def _is_thinking_rejection(exc: Exception) -> bool:
    """Whether `exc` is the API refusing the `thinking` parameter we sent.

    As narrow as `_is_temperature_rejection` for the same reason: a `thinking`
    block the model would accept in another shape is a real configuration error
    and must surface, not be papered over by a retry that drops the setting.
    """
    if getattr(exc, "status_code", None) != 400:
        return False
    message = str(exc).lower()
    if "thinking" not in message:
        return False
    return any(
        phrase in message
        for phrase in ("deprecated", "not supported", "unsupported", "unexpected", "removed")
    )


def _is_effort_rejection(exc: Exception) -> bool:
    """Whether `exc` is the API refusing the `output_config` we sent."""
    if getattr(exc, "status_code", None) != 400:
        return False
    message = str(exc).lower()
    return "effort" in message or "output_config" in message


def _is_temperature_rejection(exc: Exception) -> bool:
    """Whether `exc` is the API refusing to accept `temperature` at all.

    Narrower than "the message mentions temperature": a value the model would
    accept in a different range is a real configuration error and must surface,
    not be papered over by a retry that quietly drops the setting.
    """
    if getattr(exc, "status_code", None) != 400:
        return False
    message = str(exc).lower()
    if "temperature" not in message:
        return False
    return any(
        phrase in message
        for phrase in ("deprecated", "not supported", "unsupported", "unexpected", "removed")
    )


def _replayable_block(block: Any) -> dict[str, Any] | None:
    """A reasoning block in the shape the API needs it sent back in.

    A tool loop replays the assistant turn it just received, and when thinking
    is on the API requires the thinking blocks back unchanged — signature
    included — ahead of the tool_use blocks that followed them. Dropping them
    fails the loop's second iteration, so this reaches for the SDK's own
    serialisation first and only rebuilds the dict by hand if the object cannot
    do it. A block neither route can reproduce faithfully is left out: a
    malformed block is refused just as a missing one is, and the caller keeps
    the better error.
    """
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        try:
            data = dump(exclude_none=True)
        except Exception:  # not a pydantic model after all
            data = None
        if isinstance(data, dict) and data.get("type"):
            return data
    btype = getattr(block, "type", None)
    if btype == "thinking":
        text = getattr(block, "thinking", None)
        signature = getattr(block, "signature", None)
        if text is not None and signature is not None:
            return {"type": "thinking", "thinking": text, "signature": signature}
    elif btype == "redacted_thinking":
        data = getattr(block, "data", None)
        if data is not None:
            return {"type": "redacted_thinking", "data": data}
    return None


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        import anthropic

        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    # ── Request shaping ─────────────────────────────────────────────────────

    def _request_params(self, messages: list[dict], system: str, kwargs: dict) -> dict[str, Any]:
        """The common request body, with each optional knob only where it is legal."""
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": kwargs.get("max_tokens", 16384),
            "system": system,
            "messages": messages,
        }
        if not _rejects_temperature(self.model):
            params.update(_temp_params(kwargs))
        reasoning_effort = kwargs.get("reasoning_effort")
        thinking = self._thinking_param(reasoning_effort)
        if thinking is not None:
            params["thinking"] = thinking
        # Effort rides with thinking and only with thinking. Beyond keeping the
        # two consistent, this is what keeps us off Claude Opus 5's one illegal
        # combination: it accepts `disabled` at effort `high` or below and 400s
        # at `xhigh`/`max`, and a request that disables thinking sends no effort
        # at all, so the default (`high`) applies.
        if thinking != {"type": "disabled"}:
            effort = _effort_for(self.model, reasoning_effort)
            if effort is not None:
                params["output_config"] = {"effort": effort}
        return params

    def _thinking_param(self, reasoning_effort: str | None) -> dict[str, str] | None:
        """The `thinking` block for this request, or None to omit the parameter.

        Every caller in this codebase asks for `reasoning_effort="none"`, which
        the OpenAI-shaped providers honour. Before Claude Opus 5 that request
        was satisfied by silence, so this provider never had to send anything;
        on the models in `_THINKING_ON_BY_DEFAULT` silence now means the
        opposite, and the request has to say so.
        """
        if not _thinks_by_default(self.model):
            # Thinking is already off unless the caller opts in.
            return {"type": "adaptive"} if reasoning_effort not in (None, "none") else None
        if reasoning_effort not in (None, "none"):
            return {"type": "adaptive"}
        if not _can_disable_thinking(self.model):
            logger.warning(
                "[anthropic] %s cannot disable reasoning; its thinking tokens are "
                "billed against max_tokens, so a small budget may return no text at all",
                self.model,
            )
            return None
        return {"type": "disabled"}

    # ── Degrading a request the model refused ───────────────────────────────

    def _degrade(self, exc: Exception, params: dict[str, Any]) -> bool:
        """Whether dropping a parameter this model rejected earns a second try.

        Mutates `params` when it does. One parameter per attempt: a request
        that would need two of these dropped is a request we have got wrong in
        a way the caller should see.
        """
        return (
            self._retry_without_temperature(exc, params)
            or self._retry_without_thinking(exc, params)
            or self._retry_without_effort(exc, params)
        )

    def _retry_without_thinking(self, exc: Exception, params: dict[str, Any]) -> bool:
        """Whether dropping `thinking` gives this request a second chance.

        A model released after this file may refuse the parameter outright, or
        refuse the `adaptive` shape it is too old to know. Losing the whole turn
        is worse than reasoning we did not ask for, so drop the parameter,
        remember the model, and let the caller see a real answer.
        """
        if "thinking" not in params or not _is_thinking_rejection(exc):
            return False
        params.pop("thinking")
        _learned_no_thinking_param.add(self.model)
        logger.warning(
            "[anthropic] %s rejected the `thinking` parameter (%s); retrying without it "
            "and omitting it for the rest of this process. Reasoning tokens count "
            "against max_tokens, so tight budgets may return an empty response",
            self.model,
            exc,
        )
        return True

    def _retry_without_effort(self, exc: Exception, params: dict[str, Any]) -> bool:
        """Whether dropping `output_config` gives this request a second chance.

        Depth is a preference; the answer is not. A model whose accepted levels
        this file names wrongly costs one 400 and then runs at the API default.
        """
        if "output_config" not in params or not _is_effort_rejection(exc):
            return False
        params.pop("output_config")
        _learned_no_effort.add(self.model)
        logger.warning(
            "[anthropic] %s does not accept this `output_config.effort` (%s); retrying "
            "without it and omitting it for the rest of this process",
            self.model,
            exc,
        )
        return True

    def _retry_without_temperature(self, exc: Exception, params: dict[str, Any]) -> bool:
        """Whether dropping `temperature` gives this request a second chance.

        Mutates `params` when it does, and remembers the model so the rest of
        the process omits the parameter from the start.
        """
        if "temperature" not in params or not _is_temperature_rejection(exc):
            return False
        params.pop("temperature")
        _learned_no_sampling.add(self.model)
        logger.warning(
            "[anthropic] %s does not accept `temperature` (%s); retrying without it "
            "and omitting it for the rest of this process",
            self.model,
            exc,
        )
        return True

    async def _create(self, params: dict[str, Any]):
        """`messages.create`, retried once without whatever the model refused."""
        try:
            return await self.client.messages.create(**params)  # pyright: ignore[reportCallIssue,reportArgumentType]
        except Exception as exc:  # re-raised unless it is one of the cases we handle
            if not self._degrade(exc, params):
                raise
        return await self.client.messages.create(**params)  # pyright: ignore[reportCallIssue,reportArgumentType]

    def _extract_text(self, response) -> str:
        """Join every text block, skipping thinking / redacted_thinking and any
        other non-text block. Robust whether or not extended thinking is enabled;
        without this, content[0] is a ThinkingBlock and .text raises.

        A response made entirely of thinking blocks is the failure mode this
        provider is easiest to hit and hardest to see: nothing raises, and the
        caller receives "". Callers that coerce a falsy answer to a default —
        the intent classifier turns it into "OTHER" — then misroute in silence.
        Say so once, here, where the reason is still visible.
        """
        parts: list[str] = []
        saw_thinking = False
        for block in getattr(response, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                parts.append(getattr(block, "text", "") or "")
            elif btype in ("thinking", "redacted_thinking"):
                saw_thinking = True
        text = "".join(parts).strip()
        if not text and saw_thinking:
            logger.warning(
                "[anthropic] %s returned only reasoning and no text (stop_reason=%s, "
                "output_tokens=%s). The token budget was spent thinking; raise max_tokens "
                "or disable reasoning for this call site",
                self.model,
                getattr(response, "stop_reason", None),
                getattr(getattr(response, "usage", None), "output_tokens", None),
            )
        return text

    async def _complete(self, messages: list[dict], system: str = "", **kwargs) -> tuple[str, dict]:
        response = await self._create(self._request_params(messages, system, kwargs))
        text = self._extract_text(response)
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cost_usd": calc_cost(
                self.model, response.usage.input_tokens, response.usage.output_tokens
            ),
        }
        return text, usage

    async def _complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict[str, Any]],
        system: str = "",
        **kwargs: Any,
    ) -> ToolCompletion:
        params = self._request_params(self._anthropic_messages(messages), system, kwargs)
        params["tools"] = self._anthropic_tools(tools)
        response = await self._create(params)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        assistant_content: list[dict[str, Any]] = []
        for block in getattr(response, "content", []) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text = getattr(block, "text", "")
                text_parts.append(text)
                assistant_content.append({"type": "text", "text": text})
            elif btype in ("thinking", "redacted_thinking"):
                # Not shown to anyone, but the next turn of the loop replays
                # this message and the API requires them back unchanged.
                replay = _replayable_block(block)
                if replay is not None:
                    assistant_content.append(replay)
            elif btype == "tool_use":
                name = getattr(block, "name", "")
                call_id = getattr(block, "id", "")
                args = getattr(block, "input", {}) or {}
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append(ToolCall(id=call_id, name=name, arguments=args))
                assistant_content.append(
                    {"type": "tool_use", "id": call_id, "name": name, "input": args}
                )
        usage_obj = getattr(response, "usage", None)
        input_tok = getattr(usage_obj, "input_tokens", 0) if usage_obj else 0
        output_tok = getattr(usage_obj, "output_tokens", 0) if usage_obj else 0
        usage = {
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "cost_usd": calc_cost(self.model, input_tok, output_tok),
        }
        return ToolCompletion(
            content="".join(text_parts).strip(),
            usage=usage,
            tool_calls=tool_calls,
            assistant_message={"role": "assistant", "content": assistant_content},
        )

    @staticmethod
    def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("parameters", {"type": "object", "properties": {}}),
            }
            for tool in tools
        ]

    @staticmethod
    def _anthropic_messages(messages: list[dict]) -> list[dict]:
        converted: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.get("tool_call_id")
                                or message.get("id")
                                or "",
                                "content": str(message.get("content", "")),
                                "is_error": bool(message.get("is_error")),
                            }
                        ],
                    }
                )
                continue
            content = message.get("content", "")
            converted.append(
                {
                    "role": "assistant" if role == "assistant" else "user",
                    "content": content if isinstance(content, list) else str(content),
                }
            )
        return converted

    async def _stream(self, messages: list[dict], system: str = "", **kwargs):
        """Yield text chunks as they arrive. Final item is a dict with usage."""
        params = self._request_params(messages, system, kwargs)
        started = False
        try:
            async for item in self._stream_once(params):
                started = True
                yield item
            return
        except Exception as exc:  # re-raised unless it is one of the cases we handle
            # The rejection lands on the opening request, before the first
            # chunk, so retrying cannot replay output the caller already saw.
            # `started` keeps that true even if a later release moves the error.
            if started or not self._degrade(exc, params):
                raise
        async for item in self._stream_once(params):
            yield item

    async def _stream_once(self, params: dict[str, Any]):
        """One streaming attempt, from the request to the closing usage dict."""
        input_tokens = output_tokens = 0
        async with self.client.messages.stream(**params) as s:  # pyright: ignore[reportCallIssue,reportArgumentType]
            async for chunk in s.text_stream:
                yield chunk
            # Final message has usage counts
            final = await s.get_final_message()
            input_tokens = final.usage.input_tokens
            output_tokens = final.usage.output_tokens
        yield {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": calc_cost(self.model, input_tokens, output_tokens),
        }
