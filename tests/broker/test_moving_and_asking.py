"""An agent moved between main and a node, and the calls that cross between them.

Each of these is a path with a hop in it that a fake cannot stand in for: the
agent's state travels in a message, a reply comes back on a topic the asker
chose, and the node's request to main's model is signed with the node's key.
Over a real broker both ends are the production ones, so a topic, a signature
or a reply channel that one side spells differently fails here.
"""

import asyncio
from typing import Any

import pytest

from wactorz.agents.llm.base import LLMProvider
from wactorz.agents.main.actor import MainActor

from .conftest import FastNode, until

pytestmark = [pytest.mark.real_mqtt_client, pytest.mark.timeout(180)]

#: An agent that counts the tasks it has answered, and keeps the count.
COUNTER = """
async def setup(agent):
    agent.state["seen"] = int(agent.recall("seen") or 0)


async def handle_task(agent, payload):
    agent.state["seen"] += 1
    agent.persist("seen", agent.state["seen"])
    return {"result": "seen:" + str(agent.state["seen"])}
"""

#: An agent that answers a task with what it was sent.
ECHO = """
async def setup(agent):
    pass


async def handle_task(agent, payload):
    return {"result": "echo:" + str(payload.get("text", ""))}
"""

#: An agent that passes a task on to `echo`, wherever that is, and reports the answer.
ASKS_ECHO = """
async def setup(agent):
    pass


async def handle_task(agent, payload):
    reply = await agent.send_to("echo", {"text": payload.get("text", "")}, timeout=30)
    return {"result": "relayed:" + str((reply or {}).get("result"))}
"""

#: An agent that puts a task to the model and reports what it said.
ASKS_THE_MODEL = """
async def setup(agent):
    pass


async def handle_task(agent, payload):
    said = await agent.ask_llm(str(payload.get("text", "")), timeout=30)
    return {"result": "model:" + said}
"""


def _agents_main_sees(main: MainActor, node: FastNode) -> list[str]:
    return list(main._known_nodes.get(node.node_name, {}).get("agents", []))


async def _ask(main: MainActor, name: str, text: str = "") -> Any:
    reply = await asyncio.wait_for(
        main.delegation.delegate_task(name, text, timeout=30), timeout=60
    )
    assert reply is not None, f"'{name}' never answered"
    return reply.get("result")


async def _on_main(main: MainActor, name: str, code: str) -> None:
    registry = main._registry
    assert registry is not None
    await main._spawn_local_from_config(
        {"name": name, "type": "dynamic", "code": code}, blocking_install=True
    )
    await until(lambda: registry.find_by_name(name) is not None, f"'{name}' running on main")


async def _on_the_node(main: MainActor, node: FastNode, name: str, code: str) -> None:
    await main._spawn_remote(
        {"name": name, "type": "dynamic", "code": code}, node.node_name, save=True
    )
    await until(lambda: name in _agents_main_sees(main, node), f"main seeing '{name}' on the node")


class TestAnAgentMovedOutAndHome:
    async def test_it_runs_where_it_was_sent_with_what_it_remembered(
        self, main: MainActor, node: FastNode
    ) -> None:
        registry = main._registry
        assert registry is not None
        await _on_main(main, "counter", COUNTER)
        assert await _ask(main, "counter") == "seen:1"
        assert await _ask(main, "counter") == "seen:2"

        out = await main.migrate_agent("counter", node.node_name)

        assert out["success"], out
        await until(lambda: node.get("counter") is not None, "the node running 'counter'")
        await until(lambda: registry.find_by_name("counter") is None, "main no longer running it")
        await until(
            lambda: "counter" in _agents_main_sees(main, node), "main seeing 'counter' on the node"
        )
        assert await _ask(main, "counter") == "seen:3"

        home = await main.migrate_agent("counter", "local")

        assert home["success"], home
        await until(lambda: registry.find_by_name("counter") is not None, "main running it again")
        await until(lambda: node.get("counter") is None, "the node no longer running it")
        await until(
            lambda: "counter" not in _agents_main_sees(main, node), "main seeing it gone there"
        )
        assert await _ask(main, "counter") == "seen:4"


class TestAnAgentOnMainAsksOneOnANode:
    async def test_the_answer_comes_back_to_the_agent_that_asked(
        self, main: MainActor, node: FastNode
    ) -> None:
        await _on_the_node(main, node, "echo", ECHO)
        await _on_main(main, "relay", ASKS_ECHO)

        assert await _ask(main, "relay", "over") == "relayed:echo:over"


class _Scripted(LLMProvider):
    """A model on main that answers with what it was asked, and counts the calls."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[str] = []

    async def _complete(
        self, messages: list[dict], system: str = "", **kwargs: Any
    ) -> tuple[str, dict]:
        prompt = str(messages[-1]["content"])
        self.asked.append(prompt)
        return f"heard {prompt}", {"input_tokens": 3, "output_tokens": 2}


class TestAnAgentOnANodeAsksTheModel:
    async def test_main_makes_the_call_and_the_node_gets_the_answer(
        self, main: MainActor, node: FastNode
    ) -> None:
        # The node holds no key for a model. Its request goes to main signed
        # with the node's key, and main's answer comes back on a reply topic
        # under the node's own name.
        model = _Scripted()
        main.llm = model
        spent = main.total_input_tokens
        await _on_the_node(main, node, "thinker", ASKS_THE_MODEL)

        assert await _ask(main, "thinker", "the weather") == "model:heard the weather"

        assert model.asked == ["the weather"]
        assert main.total_input_tokens == spent + 3, "the call is counted where it was paid for"
