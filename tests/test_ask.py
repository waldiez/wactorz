"""One way to ask an agent and wait: ``Actor.ask`` from an agent, ``wactorz.ask``
from host code.

Both send a TASK tagged with a correlation id and a reply address, and settle
on the RESULT that echoes the id. What a caller sees is the answer: the
plumbing is stripped, an error reply is raised, an unknown agent and a silent
one are errors too, and nothing is left waiting afterwards whatever happened.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import wactorz
from wactorz import app as app_module
from wactorz.agents.function_agent import FunctionAgent, agent, spec_of
from wactorz.core.actor import Actor, ActorState, ask_payload, reply_value
from wactorz.core.registry import ActorRegistry


@agent(name="doubler", input_schema={"n": "int"}, output_schema={"doubled": "int"})
def doubler(payload: dict) -> dict:
    return {"doubled": payload["n"] * 2}


@agent(name="greeter")
def greeter(payload: dict) -> str:
    return f"hello {payload.get('text') or payload.get('name')}"


@agent(name="relay")
async def relay(payload: dict, me: FunctionAgent) -> dict:
    """Asks the doubler on the caller's behalf: an agent asking an agent."""
    return {"via": await me.ask("doubler", {"n": payload["n"]})}


@agent(name="breaker")
def breaker(payload: dict) -> dict:
    raise ValueError("the widget is jammed")


@agent(name="sleeper")
async def sleeper(payload: dict) -> dict:
    await asyncio.sleep(payload.get("seconds", 30))
    return {"woke": True}


class Live:
    """A registry of agents whose message loops run, and nothing else of a start.

    The full start also opens the broker link and the heartbeat, which these
    tests have no use for; the mailbox loop is what delivers a task and lets
    the agent answer it.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.registry = ActorRegistry()
        self.actors: dict[str, Actor] = {}
        self._loops: list[asyncio.Task[Any]] = []
        self._tmp_path = tmp_path

    async def add(self, fn: Any) -> Actor:
        spec = spec_of(fn)
        assert spec is not None
        actor = spec.build(persistence_dir=str(self._tmp_path))
        actor.state = ActorState.RUNNING
        await self.registry.register(actor)
        self._loops.append(asyncio.create_task(actor._message_loop()))
        self.actors[actor.name] = actor
        return actor

    async def close(self) -> None:
        for task in self._loops:
            task.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)


@pytest.fixture(name="live")
async def live_fixture(tmp_path: Path) -> AsyncIterator[Live]:
    live = Live(tmp_path)
    for fn in (doubler, greeter, relay, breaker, sleeper):
        await live.add(fn)
    yield live
    await live.close()


class TestAnAgentAsking:
    async def test_the_reply_is_the_functions_return_value(self, live: Live) -> None:
        assert await live.actors["relay"].ask("doubler", {"n": 4}) == {"doubled": 8}

    async def test_an_agent_can_ask_on_a_callers_behalf(self, live: Live) -> None:
        """relay asks doubler while answering: two asks nested, both settle."""
        assert await live.registry.ask("relay", {"n": 5}) == {"via": {"doubled": 10}}

    async def test_a_non_dict_request_travels_as_text(self, live: Live) -> None:
        assert await live.actors["relay"].ask("greeter", "Sam") == {"result": "hello Sam"}

    async def test_an_error_reply_is_raised_not_returned(self, live: Live) -> None:
        with pytest.raises(RuntimeError, match="'breaker' answered with an error: the widget"):
            await live.actors["relay"].ask("breaker", {})

    async def test_an_unknown_agent_is_a_lookup_error(self, live: Live) -> None:
        with pytest.raises(LookupError, match="no agent named 'nobody'"):
            await live.actors["relay"].ask("nobody", {})

    async def test_a_silent_agent_is_a_timeout(self, live: Live) -> None:
        with pytest.raises(asyncio.TimeoutError, match=r"'sleeper' did not answer within 0\.05s"):
            await live.actors["relay"].ask("sleeper", {}, timeout=0.05)

    async def test_nothing_is_left_waiting_whatever_happened(self, live: Live) -> None:
        asker = live.actors["relay"]
        await asker.ask("doubler", {"n": 1})
        with pytest.raises(RuntimeError):
            await asker.ask("breaker", {})
        with pytest.raises(asyncio.TimeoutError):
            await asker.ask("sleeper", {}, timeout=0.05)
        assert asker._result_futures == {}

    async def test_without_a_registry_it_says_so(self, tmp_path: Path) -> None:
        spec = spec_of(doubler)
        assert spec is not None
        loner = spec.build(persistence_dir=str(tmp_path))
        with pytest.raises(RuntimeError, match="no registry"):
            await loner.ask("doubler", {"n": 1})

    async def test_a_full_mailbox_is_refused_rather_than_waited_on(self, live: Live) -> None:
        asker = live.actors["relay"]

        async def refuse(*_args: Any, **_kwargs: Any) -> bool:
            return False

        asker.send = refuse  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(RuntimeError, match="not taking messages"):
            await asker.ask("doubler", {"n": 1})
        assert asker._result_futures == {}


class TestHostCodeAsking:
    """``wactorz.ask`` reaches the running system's registry; the reply address
    is a slot of the registry, so no actor is registered for the caller."""

    @pytest.fixture(autouse=True)
    def _system(self, live: Live, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app_module, "_current_system", SimpleNamespace(registry=live.registry))

    async def test_it_answers_like_an_agents_ask(self, live: Live) -> None:
        assert await wactorz.ask("doubler", {"n": 21}) == {"doubled": 42}
        assert await wactorz.ask("greeter", {"name": "Ada"}) == {"result": "hello Ada"}

    async def test_the_same_three_failures(self, live: Live) -> None:
        with pytest.raises(RuntimeError, match="answered with an error"):
            await wactorz.ask("breaker", {})
        with pytest.raises(LookupError):
            await wactorz.ask("nobody", {})
        with pytest.raises(asyncio.TimeoutError):
            await wactorz.ask("sleeper", {}, timeout=0.05)

    async def test_no_slot_and_no_actor_is_left_behind(self, live: Live) -> None:
        before = set(live.registry.all_actors())
        await wactorz.ask("doubler", {"n": 1})
        with pytest.raises(asyncio.TimeoutError):
            await wactorz.ask("sleeper", {}, timeout=0.05)
        assert live.registry._reply_slots == {}
        assert set(live.registry.all_actors()) == before

    async def test_two_asks_in_flight_each_get_their_own_answer(self, live: Live) -> None:
        a, b = await asyncio.gather(
            wactorz.ask("doubler", {"n": 1}), wactorz.ask("doubler", {"n": 2})
        )
        assert (a, b) == ({"doubled": 2}, {"doubled": 4})


async def test_without_a_running_system_host_code_is_told(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "_current_system", None)
    with pytest.raises(RuntimeError, match="no system is running"):
        await wactorz.ask("doubler", {"n": 1})


class TestTheShapes:
    def test_the_request_carries_the_two_tags_and_nothing_of_the_callers_is_lost(self) -> None:
        body = ask_payload({"n": 1}, task_id="t1", reply_to="me")
        assert body == {"n": 1, "_task_id": "t1", "_reply_to": "me"}

    def test_a_plain_value_becomes_text(self) -> None:
        assert ask_payload(42, task_id="t", reply_to="me")["text"] == "42"

    def test_the_reply_loses_its_correlation_id_in_either_spelling(self) -> None:
        assert reply_value({"x": 1, "_task_id": "t"}, task_id="t", target="a") == {"x": 1}
        assert reply_value({"x": 1, "task": "t"}, task_id="t", target="a") == {"x": 1}

    def test_an_agents_own_task_field_is_kept(self) -> None:
        """An LLM agent echoes the task text under "task"; that is its answer, not the id."""
        reply = {"text": "done", "task": "summarise this", "_task_id": "t"}
        assert reply_value(reply, task_id="t", target="a") == {
            "text": "done",
            "task": "summarise this",
        }

    def test_an_empty_error_is_not_an_error(self) -> None:
        """Generated agents are told to return {"error": null} on success."""
        assert reply_value({"result": 1, "error": None}, task_id="t", target="a") == {
            "result": 1,
            "error": None,
        }

    def test_a_non_dict_reply_is_returned_as_it_is(self) -> None:
        assert reply_value("ok", task_id="t", target="a") == "ok"
