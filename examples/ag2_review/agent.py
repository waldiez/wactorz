"""An AG2 (AutoGen) conversation as a Wactorz agent.

Two AG2 agents, a writer and a critic, improve a draft between them. Wactorz
runs that conversation for every draft published on a topic, keeps it
supervised, and shows what it costs. Written for AG2 0.x, whose package is
`autogen`; the system's chat channels and planner stay as they are.
"""

import os
from typing import Any

from autogen import ConversableAgent

import wactorz
from wactorz.config import CONFIG
from wactorz.core.integrations.ag2 import record_usage

#: Rounds between the writer and the critic before the draft is returned.
MAX_TURNS = 3


#: AG2's `api_type` for each of the system's providers, where it is not the
#: OpenAI-compatible default. Anthropic and Google need AG2's extra of the same
#: name (`pip install 'ag2[anthropic]'`), which is the provider's own SDK.
API_TYPES = {"AnthropicProvider": "anthropic", "GeminiProvider": "google"}


def llm_config(me: wactorz.FunctionAgent) -> dict[str, Any] | bool:
    """AG2's model configuration, from the model the system runs on.

    The same provider Wactorz is configured with, so `LLM_PROVIDER` and its key
    drive the conversation too. ``False`` when there is no model: the two
    agents then exchange their default replies, which shows the plumbing.
    """
    provider = getattr(me.llm, "provider", me.llm)
    kind = type(provider).__name__
    model = str(getattr(provider, "model", "") or "")
    key = CONFIG.llm_api_key
    entry: dict[str, Any] | None = None
    if kind == "AnthropicProvider":
        entry = {"model": model, "api_key": os.environ.get("ANTHROPIC_API_KEY") or key}
    elif kind == "OpenAIProvider":
        entry = {"model": model, "api_key": os.environ.get("OPENAI_API_KEY") or key}
        if CONFIG.openai_url:
            entry["base_url"] = CONFIG.openai_url
    elif kind == "OllamaProvider":
        # Ollama speaks the OpenAI protocol under /v1, free of charge.
        base = str(getattr(provider, "base_url", "") or CONFIG.ollama_url).rstrip("/")
        entry = {"model": model, "base_url": f"{base}/v1", "api_key": "ollama", "price": [0, 0]}
    elif kind == "NIMProvider":
        entry = {
            "model": model,
            "base_url": "https://integrate.api.nvidia.com/v1",
            "api_key": CONFIG.nim_api_key or CONFIG.nvidia_api_key or key,
        }
    elif kind == "GeminiProvider":
        entry = {
            "model": model,
            "api_key": os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or key,
        }
    elif os.environ.get("OPENAI_API_KEY"):
        entry = {
            "model": os.environ.get("AG2_MODEL", "gpt-4o-mini"),
            "api_key": os.environ["OPENAI_API_KEY"],
        }
    if entry is None or not entry.get("api_key"):
        return False
    if kind in API_TYPES:
        entry["api_type"] = API_TYPES[kind]
    return {"config_list": [entry]}


def make_agents(config: dict[str, Any] | bool) -> tuple[ConversableAgent, ConversableAgent]:
    """The writer and the critic, fresh for one draft."""
    writer = ConversableAgent(
        "writer",
        system_message="You improve the draft you are given. Reply with the improved draft only.",
        llm_config=config,
        human_input_mode="NEVER",
        default_auto_reply="(no model: the draft stands as written)",
    )
    critic = ConversableAgent(
        "critic",
        system_message=(
            "You review drafts. Point out at most three concrete problems. "
            "When the draft is good, say so and end with TERMINATE."
        ),
        llm_config=config,
        human_input_mode="NEVER",
        default_auto_reply="Looks fine. TERMINATE",
        is_termination_msg=lambda message: "TERMINATE" in str(message.get("content", "")),
    )
    return writer, critic


@wactorz.agent(
    name="ag2-review",
    subscribes="drafts/new",
    publishes="drafts/reviewed",
    description="Improves a draft in an AG2 writer–critic conversation.",
    capabilities=["review", "ag2"],
    input_schema={"id": "str", "text": "str"},
    output_schema={"id": "str", "text": "str", "turns": "int", "cost_usd": "float"},
    requires={"packages": ["ag2"]},
    concurrency=2,
)
async def review(draft: dict, me: wactorz.FunctionAgent) -> dict | None:
    """Run one draft through the conversation; what the writer last said is the result."""
    text = str(draft.get("text", "")).strip()
    if not text:
        return None
    config = me.options.get("llm_config", llm_config(me))
    if config is False:
        await me.log(
            "No model: the writer and the critic exchange their default replies, and the "
            "draft comes back as it is. Start with LLM_PROVIDER set to have it reviewed.",
            level="warning",
        )
    writer, critic = make_agents(config)
    result = await writer.a_initiate_chat(
        critic, message=text, max_turns=int(me.options.get("max_turns", MAX_TURNS)), silent=True
    )
    cost = record_usage(me, [writer, critic])
    final = next(
        (m["content"] for m in reversed(result.chat_history) if m.get("name") == "writer"), text
    )
    return {
        "id": draft.get("id"),
        "text": final,
        "turns": len(result.chat_history),
        "cost_usd": cost,
    }
