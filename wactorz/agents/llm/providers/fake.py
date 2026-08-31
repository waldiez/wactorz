"""A provider that answers deterministically, for end-to-end runs and demos.

Not a mock: it is a real ``LLMProvider`` reached through the ordinary factory, so
a system built on it exercises the same call path, the same spend accounting and
the same streaming machinery as one built on a paid model. What it does not do is
think — every answer is decided by the request, so the same scenario produces the
same run.

**It answers by contract rather than by echo.** Two call sites demand a specific
shape and treat anything else as a parse failure: the intent classifier wants one
of four tokens, and fact extraction wants JSON. An echo satisfies neither, so a
system on an echoing provider takes a different branch each turn — the opposite
of what a fake is for. Those two are matched against the prompt constants
themselves, so rewording a prompt cannot silently stop the routing working.

Anything else is conversation, and answers from a script: a mapping of substring
to reply, with a default. Tests take the default; a recorded walkthrough loads a
script whose answers read well. The provider is the same either way.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Mapping

from ...prompts.main_actor_prompts import FACTS_EXTRACT_PROMPT, INTENT_CLASSIFIER_PROMPT
from ..base import LLMProvider

#: The only answers the intent classifier accepts. `OTHER` routes to ordinary
#: chat, which is what nearly every scenario wants.
INTENTS = ("ACTUATE", "HA", "PIPELINE", "OTHER")

#: What fact extraction means by "nothing durable was stated".
NO_FACTS = "{}"

#: Said when no script entry matches. Deliberately bland and obviously synthetic:
#: a default that reads like a real answer is one that reaches a recording
#: without anyone noticing the model never ran.
DEFAULT_REPLY = "This is a scripted reply from the fake LLM provider."

#: Characters per streamed chunk. Small enough that a scenario asserting
#: "streamed" sees more than one chunk for any real sentence.
CHUNK_SIZE = 24

#: Cost per token, in USD. Tiny but **not zero**, and that matters: a provider
#: reporting zero would make "the cost total moves while an agent is answering"
#: pass without the total ever moving, which is the assertion that exists because
#: a regression once froze that number while every test stayed green.
USD_PER_TOKEN = 1e-06


class FakeProvider(LLMProvider):
    """Deterministic answers, honest usage, real streaming."""

    def __init__(
        self,
        model: str | None = None,
        script: Mapping[str, str] | None = None,
        intent: str = "OTHER",
    ) -> None:
        """``script`` maps a substring of the last user message to a reply.

        ``intent`` is what the classifier is told, so a scenario can drive the
        planner or the actuator without saying anything the classifier would have
        to interpret.
        """
        if intent not in INTENTS:
            msg = f"intent must be one of {INTENTS}, got {intent!r}"
            raise ValueError(msg)
        self.model = model or "fake"
        self.script = dict(script or {})
        self.intent = intent
        #: Every (system, messages) pair this provider was asked, in order. A
        #: scenario asserting "the model was asked at all" needs this; without it
        #: a silent no-call and a silent empty answer look identical.
        self.calls: list[tuple[str, list[dict]]] = []

    # ── The contract-bound answers ──────────────────────────────────────────

    def _answer(self, messages: list[dict], system: str) -> str:
        """What this request gets, decided by which call site is asking."""
        self.calls.append((system, list(messages)))
        if system == INTENT_CLASSIFIER_PROMPT:
            return self.intent
        if system == FACTS_EXTRACT_PROMPT:
            return NO_FACTS
        return self._scripted(_last_user_text(messages))

    def _scripted(self, text: str) -> str:
        """First script entry whose key appears in the text, else the default.

        Longest key first, so a specific phrase wins over a substring of itself
        and adding a broad entry cannot shadow the precise ones already there.
        """
        for key in sorted(self.script, key=len, reverse=True):
            if key.lower() in text.lower():
                return self.script[key]
        return DEFAULT_REPLY

    # ── Provider surface ────────────────────────────────────────────────────

    async def _complete(
        self, messages: list[dict], system: str = "", **kwargs: object
    ) -> tuple[str, dict]:
        text = self._answer(messages, system)
        return text, _usage(messages, system, text)

    async def _stream(
        self, messages: list[dict], system: str = "", **kwargs: object
    ) -> AsyncGenerator[str, None]:
        text = self._answer(messages, system)
        for start in range(0, len(text), CHUNK_SIZE):
            yield text[start : start + CHUNK_SIZE]


def _last_user_text(messages: list[dict]) -> str:
    """The most recent user turn, flattened to text.

    Content may be a list of blocks once attachments are involved, so the text
    parts are joined and everything else ignored — a fake has no use for an
    image, but it must not crash on one.
    """
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return " ".join(p for p in parts if p)
        return str(content)
    return ""


def _tokens(text: str) -> int:
    """A token count that is proportional to the text and never zero.

    Not an accurate tokenizer and not trying to be: what the counters need is a
    number that grows with the work done, so an assertion about totals moving
    has something to observe. A minimum of one keeps an empty prompt from
    reporting a free call.
    """
    return max(1, len(text) // 4)


def _usage(messages: list[dict], system: str, reply: str) -> dict:
    """The usage shape every provider returns, with a non-zero cost."""
    prompt_text = system + " ".join(str(m.get("content", "")) for m in messages)
    input_tokens = _tokens(prompt_text)
    output_tokens = _tokens(reply)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": round((input_tokens + output_tokens) * USD_PER_TOKEN, 10),
    }


def parse_script(raw: str) -> dict[str, str]:
    """A script from JSON, for handing one to a provider through the environment.

    Returns an empty script for empty input or anything that is not a JSON
    object of strings — a malformed script falls back to the default reply
    rather than failing a run, since the reply text is never what is under test.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}
