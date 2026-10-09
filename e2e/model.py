"""What the model says in this suite: every question a journey asks it, and its answer.

The application runs on its scripted provider (``LLM_PROVIDER=fake``), which
answers a message containing one of these keys with the text beside it, and
anything else with a sentence saying it had no script. So a journey knows word
for word what an agent will say, and an agent that asks the model something no
journey planned for is caught by the guard.

Kept as Python and not as JSON: the answers that start an agent carry its
program, and a program is readable as a string here and not as one escaped line.
"""

from __future__ import annotations

import datetime
import json

from harness.run import NODE_NAME

#: An agent that counts what it is sent, and keeps the count.
COUNTER = """
async def setup(agent):
    agent.state["seen"] = int(agent.recall("seen") or 0)


async def handle_task(agent, payload):
    agent.state["seen"] += 1
    agent.persist("seen", agent.state["seen"])
    return {"result": "counted " + str(agent.state["seen"])}
"""

#: An agent that ends itself a few seconds after it starts, as a planner or a
#: one-off actuator does once its work is done.
FINISHER = """
async def setup(agent):
    agent.state["ticks"] = 0


async def process(agent):
    agent.state["ticks"] += 1
    if agent.state["ticks"] >= 8:
        await agent.stop()
        return
"""

#: An agent that puts what it is sent to the model, and says what came back.
ASKS_THE_MODEL = """
async def setup(agent):
    pass


async def handle_task(agent, payload):
    said = await agent.ask_llm(str(payload.get("text", "")), timeout=30)
    return {"result": "the model said: " + said}
"""


#: A moment already past when any journey asks. With its offset, so it means
#: the same moment whatever time zone the server reads schedules in.
_JUST_NOW = (
    datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
).isoformat(timespec="seconds")


def _starts(words: str, **agent: object) -> str:
    """A reply from main that starts an agent: what it says, then the block that asks for it."""
    return f"{words}\n<spawn>\n{json.dumps(agent)}\n</spawn>"


SCRIPT = {
    "say hello": "Hello from the scripted model.",
    "start a greeter": _starts(
        "I'll set that up.",
        name="greeter",
        type="llm",
        description="Greets whoever writes to it",
        capabilities=["greeting"],
        system_prompt="You greet people. Answer in one short sentence.",
    ),
    "good morning": "Good morning to you too.",
    "start a counter on the node": _starts(
        "Starting it there.",
        name="counter",
        type="dynamic",
        node=NODE_NAME,
        description="Counts the messages it is sent",
        capabilities=["counting"],
        code=COUNTER,
    ),
    "start an asker on the node": _starts(
        "Starting it there.",
        name="asker",
        type="dynamic",
        node=NODE_NAME,
        description="Asks the model what it is asked",
        capabilities=["asking"],
        code=ASKS_THE_MODEL,
    ),
    "how is the tide": "The tide is in.",
    # Asked of a planner, which finds no plan in it and answers directly: the
    # shortest run a planner has, from its start to its answer. Main adds the
    # last few exchanges to a short planner task, and the longest key found
    # wins, so this one is longer than any that conversation could contain.
    "count the beans in the old blue jar": "There are seven beans.",
    "start a finisher here": _starts(
        "Starting it.",
        name="finisher",
        type="dynamic",
        description="Does one thing and ends itself",
        capabilities=["finishing"],
        poll_interval=1,
        code=FINISHER,
    ),
    # A once-schedule whose moment is already past: it fires if that was within
    # its catch-up window, and ends itself either way.
    "start a reminder here": _starts(
        "Setting it.",
        name="reminder",
        type="scheduled",
        description="Reminds once",
        schedule={"type": "once", "at": _JUST_NOW},
    ),
    "start a finisher on the node": _starts(
        "Starting it there.",
        name="far-finisher",
        type="dynamic",
        node=NODE_NAME,
        description="Does one thing and ends itself",
        capabilities=["finishing"],
        poll_interval=1,
        code=FINISHER,
    ),
}


def as_json() -> str:
    """The script as the application takes it, in ``LLM_FAKE_SCRIPT``."""
    return json.dumps(SCRIPT)
