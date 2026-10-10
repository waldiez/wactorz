"""An AG2 conversation as a Wactorz agent.

Two AG2 agents, a writer and a critic, improve a draft between them. Wactorz
runs that conversation for every draft published on a topic, keeps it
supervised, and shows what it costs. The AG2 agents answer with the model
Wactorz is configured with; without one they run on AG2's test client with
canned replies, which shows the plumbing and nothing more. Written for AG2
1.x (``import ag2``).
"""

from typing import Any

from ag2 import Agent
from ag2.testing import TestConfig

import wactorz
from wactorz.core.integrations.ag2 import model_config, record_reply

#: Rounds of critique and rewrite before the draft is returned as it stands.
MAX_TURNS = 3

#: What the critic says when there is nothing left to fix.
APPROVED = "APPROVED"

#: Dollars per million input and output tokens, by model family. AG2 reports
#: the model the provider resolved to (`gpt-4o-mini-2024-07-18`), which a
#: family name matches as a prefix. Add the models you use.
PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
}


def make_agents(config: Any) -> tuple[Agent, Agent]:
    """The writer and the critic, fresh for one draft, on ``config``.

    ``None`` means no model: AG2's test client answers with canned turns, so
    the draft comes back as it is after one round.
    """
    if config is None:
        writer_config: Any = TestConfig("(no model: the draft stands as written)")
        critic_config: Any = TestConfig(f"Looks fine. {APPROVED}")
    else:
        writer_config = critic_config = config
    writer = Agent(
        "writer",
        "You improve the draft you are given. Reply with the improved draft only, no preamble.",
        config=writer_config,
    )
    critic = Agent(
        "critic",
        "You review drafts. Point out at most three concrete problems, briefly. "
        f"When the draft is good, say so and end with {APPROVED}.",
        config=critic_config,
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
    """Run one draft through the conversation; the writer's last version is the result.

    The writer rewrites, the critic reviews the rewrite, and the writer takes
    the critique, each in a conversation of its own that lasts the whole
    review, until the critic approves or the rounds run out. So the writer
    sees its earlier versions and the critic whether its points were taken.
    """
    text = str(draft.get("text", "")).strip()
    if not text:
        return None
    config = me.options.get("config", model_config(me))
    if config is None:
        await me.log(
            "No model: the writer and the critic answer with canned replies, and the draft "
            "comes back as it is. Start with LLM_PROVIDER set to have it reviewed.",
            level="warning",
        )
    writer, critic = make_agents(config)
    max_turns = int(me.options.get("max_turns", MAX_TURNS))

    reply = await writer.ask(f"Improve this draft:\n\n{text}")
    verdict = await critic.ask(f"Review this draft:\n\n{reply.body}")
    turns = 2
    while APPROVED not in verdict.body and turns < 2 * max_turns:
        reply = await reply.ask(
            f"A reviewer said:\n\n{verdict.body}\n\nRevise the draft accordingly."
        )
        verdict = await verdict.ask(f"The revised draft:\n\n{reply.body}")
        turns += 2
    # A reply's usage covers its whole conversation, so each is counted once, at the end.
    cost = await record_reply(me, reply, prices=PRICES)
    cost += await record_reply(me, verdict, prices=PRICES)
    me.persist("drafts_total", int(me.recall("drafts_total", 0)) + 1)
    return {"id": draft.get("id"), "text": reply.body, "turns": turns, "cost_usd": round(cost, 6)}
