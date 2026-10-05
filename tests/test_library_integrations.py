"""What a chain, a graph or a job needs from an agent: its own directory, more
than one message at a time, and a way to report spend made outside the system's
providers."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.function_agent import FunctionAgent, agent, spec_of
from wactorz.agents.llm import cost as cost_module
from wactorz.core import subscriptions as subscriptions_module
from wactorz.core.actor import Actor, Message, MessageType


class Probe(Actor):
    async def handle_message(self, msg: Message) -> None:
        return None


class TestStateDir:
    def test_an_actor_has_its_own_directory_under_the_state_directory(self, tmp_path: Path) -> None:
        actor = Probe(name="pump-watch", persistence_dir=str(tmp_path))

        assert actor.state_dir == tmp_path / "pump-watch"
        assert actor.state_dir.is_dir(), "exists from construction, so a file can go in at once"
        (actor.state_dir / "weights.bin").write_bytes(b"\x00")

    def test_a_decorated_function_sees_it_on_the_actor(self, tmp_path: Path) -> None:
        @agent
        def keep(payload: dict, me: FunctionAgent) -> str:
            return str(me.state_dir)

        actor = spec_of(keep).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]
        assert keep({}, actor) == str(tmp_path / "keep")


class TestRecordLlmCost:
    @pytest.fixture
    def ledger(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        recorded: list[float] = []
        monkeypatch.setattr(cost_module, "accumulate_global_cost", recorded.append)
        return recorded

    def test_it_counts_on_the_actor_and_in_the_global_ledger(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        actor = Probe(name="explorer", persistence_dir=str(tmp_path))

        actor.record_llm_cost(0.0012, input_tokens=300, output_tokens=40, model="gpt-x")
        actor.record_llm_cost(0.0008, input_tokens=100, output_tokens=10)

        assert actor.metrics.llm_calls == 2
        assert actor.metrics.llm_input_tokens == 400
        assert actor.metrics.llm_output_tokens == 50
        assert actor.metrics.llm_cost_usd == pytest.approx(0.002)
        assert ledger == [pytest.approx(0.0012), pytest.approx(0.0008)]

    def test_the_heartbeat_shows_it_the_way_an_llm_agent_does(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        actor = Probe(name="explorer", persistence_dir=str(tmp_path))
        assert "cost_usd" not in actor._build_metrics(), "no model calls, no cost keys"

        actor.record_llm_cost(0.5, input_tokens=1, output_tokens=2)

        metrics = actor._build_metrics()
        assert metrics["cost_usd"] == 0.5
        assert (metrics["input_tokens"], metrics["output_tokens"], metrics["llm_calls"]) == (
            1,
            2,
            1,
        )

    def test_a_negative_or_absent_cost_is_counted_as_a_free_call(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        actor = Probe(name="explorer", persistence_dir=str(tmp_path))
        actor.record_llm_cost(-1.0, input_tokens=-5)
        assert actor.metrics.llm_calls == 1
        assert actor.metrics.llm_cost_usd == 0.0 and actor.metrics.llm_input_tokens == 0


class TestCallsThroughTheSystemsModelAreCounted:
    @pytest.fixture
    def ledger(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        recorded: list[float] = []
        monkeypatch.setattr(cost_module, "accumulate_global_cost", recorded.append)
        return recorded

    async def test_complete_through_me_llm_lands_on_the_card(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        from wactorz.agents.llm.providers.fake import FakeProvider

        @agent
        async def ask(payload: dict, me: FunctionAgent) -> str:
            text, _usage = await me.llm.complete([{"role": "user", "content": payload["q"]}])
            return text

        provider = FakeProvider(script={"pump": "Replace the filter."})
        actor = spec_of(ask).build(persistence_dir=str(tmp_path), llm_provider=provider)  # pyright: ignore[reportOptionalMemberAccess]

        assert await actor.call({"q": "What about the pump?"}) == "Replace the filter."

        assert actor.metrics.llm_calls == 1
        assert actor.metrics.llm_input_tokens > 0 and actor.metrics.llm_output_tokens > 0
        assert actor.metrics.llm_cost_usd > 0
        assert ledger == [pytest.approx(actor.metrics.llm_cost_usd)]
        assert actor.llm.model == provider.model, "everything else reads through to the provider"

    async def test_tool_completions_are_counted_too(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        from wactorz.agents.llm.base import ToolCompletion

        class Tools:
            model = "tooly"

            async def complete_with_tools(
                self, messages: Any, tools: Any, system: str = "", **_: Any
            ) -> ToolCompletion:
                return ToolCompletion(
                    content="", usage={"input_tokens": 5, "output_tokens": 1, "cost_usd": 0.01}
                )

        @agent
        async def plan(payload: dict, me: FunctionAgent) -> int:
            result = await me.llm.complete_with_tools([], [])
            return len(result.tool_calls)

        actor = spec_of(plan).build(persistence_dir=str(tmp_path), llm_provider=Tools())  # pyright: ignore[reportOptionalMemberAccess]
        assert await actor.call({}) == 0
        assert actor.metrics.llm_calls == 1 and actor.metrics.llm_cost_usd == pytest.approx(0.01)

    def test_without_a_provider_me_llm_is_still_none(self, tmp_path: Path) -> None:
        @agent
        def plain(payload: dict, me: FunctionAgent) -> bool:
            return me.llm is None

        actor = spec_of(plain).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]
        assert plain({}, actor) is True


class FakeHubClient:
    """A broker connection that never delivers; messages are offered by the test."""

    async def __aenter__(self) -> "FakeHubClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, qos: int = 0, **_: Any) -> None:
        return None

    @property
    def messages(self) -> Any:
        return self

    def __aiter__(self) -> "FakeHubClient":
        return self

    async def __anext__(self) -> Any:
        await asyncio.sleep(3600)
        raise StopAsyncIteration


async def _stop(actor: Actor) -> None:
    for task in actor._tasks:
        task.cancel()
    await asyncio.gather(*actor._tasks, return_exceptions=True)


class TestConcurrency:
    @pytest.fixture(autouse=True)
    def quiet_broker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subscriptions_module, "mqtt_client", lambda *a, **k: FakeHubClient())

    async def test_one_worker_keeps_order_and_handles_one_message_at_a_time(
        self, tmp_path: Path
    ) -> None:
        actor = Probe(name="serial", persistence_dir=str(tmp_path))
        running = 0
        most = 0
        seen: list[int] = []

        async def slow(payload: dict) -> None:
            nonlocal running, most
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.01)
            seen.append(payload["n"])
            running -= 1

        actor.subscribe("jobs/#", slow)
        hub = actor._sub_hub
        assert hub is not None
        for n in range(4):
            hub._bindings[0].offer({"n": n})
        await asyncio.sleep(0.1)

        assert seen == [0, 1, 2, 3] and most == 1
        await _stop(actor)

    async def test_more_workers_run_messages_of_a_topic_at_once(self, tmp_path: Path) -> None:
        actor = Probe(name="parallel", persistence_dir=str(tmp_path))
        running = 0
        most = 0
        done: list[int] = []

        async def slow(payload: dict) -> None:
            nonlocal running, most
            running += 1
            most = max(most, running)
            await asyncio.sleep(0.02)
            done.append(payload["n"])
            running -= 1

        actor.subscribe("jobs/#", slow, concurrency=3)
        hub = actor._sub_hub
        assert hub is not None
        binding = hub._bindings[0]
        assert binding.concurrency == 3 and len(binding.live_workers()) == 3
        for n in range(6):
            binding.offer({"n": n})
        await asyncio.sleep(0.1)

        assert sorted(done) == [0, 1, 2, 3, 4, 5]
        assert most == 3, "three at once, never more"
        await _stop(actor)

    async def test_stopping_the_hub_stops_every_worker(self, tmp_path: Path) -> None:
        actor = Probe(name="parallel", persistence_dir=str(tmp_path))

        async def nothing(payload: dict) -> None:
            return None

        actor.subscribe("jobs/#", nothing, concurrency=2)
        hub = actor._sub_hub
        assert hub is not None
        workers = list(hub._bindings[0].workers)
        await asyncio.sleep(0.01)

        await _stop(actor)
        await asyncio.sleep(0)

        assert len(workers) == 2 and all(w.done() for w in workers)

    def test_less_than_one_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            subscriptions_module.Binding("t", None, 10, concurrency=0)
        with pytest.raises(ValueError, match="at least 1"):

            @agent(concurrency=0)
            def bad(payload: dict) -> None:
                return None

    async def test_a_decorated_function_runs_tasks_beside_its_mailbox(self, tmp_path: Path) -> None:
        """With concurrency, a slow task does not hold the next one; without, it does."""
        started: list[str] = []

        @agent(concurrency=2)
        async def slow(payload: dict) -> dict:
            started.append(payload["id"])
            await asyncio.sleep(0.05)
            return {"done": payload["id"]}

        spec = spec_of(slow)
        assert spec is not None and spec.concurrency == 2
        actor = spec.build(persistence_dir=str(tmp_path))
        replies: list[dict] = []

        async def send(target: Any, kind: Any, payload: Any, **_: Any) -> None:
            replies.append(payload)

        actor.send = send  # pyright: ignore[reportAttributeAccessIssue]

        def task(name: str) -> Message:
            return Message(
                type=MessageType.TASK,
                sender_id="t",
                reply_to="t",
                payload={"id": name, "_task_id": name},
            )

        await actor.handle_message(task("a"))
        await actor.handle_message(task("b"))
        await asyncio.sleep(0)
        assert started == ["a", "b"], "both under way before either answered"
        await asyncio.sleep(0.1)
        assert sorted(r["done"] for r in replies) == ["a", "b"]
        assert actor.metrics.tasks_completed == 2
        await _stop(actor)

    async def test_without_concurrency_a_task_is_answered_before_the_next_starts(
        self, tmp_path: Path
    ) -> None:
        order: list[str] = []

        @agent
        async def slow(payload: dict) -> dict:
            order.append("start " + payload["id"])
            await asyncio.sleep(0.01)
            order.append("end " + payload["id"])
            return {}

        actor = spec_of(slow).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]

        async def send(*_: Any, **__: Any) -> None:
            return None

        actor.send = send  # pyright: ignore[reportAttributeAccessIssue]
        for name in ("a", "b"):
            await actor.handle_message(
                Message(type=MessageType.TASK, sender_id="t", reply_to="t", payload={"id": name})
            )

        assert order == ["start a", "end a", "start b", "end b"]


class TestLangChainCallback:
    @pytest.fixture
    def ledger(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        recorded: list[float] = []
        monkeypatch.setattr(cost_module, "accumulate_global_cost", recorded.append)
        return recorded

    def test_a_chat_result_is_reported_with_its_tokens_and_priced(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        langchain_core = pytest.importorskip("langchain_core")
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, LLMResult

        from wactorz.core.integrations.langchain import CostCallback, cost_of, token_usage

        del langchain_core
        actor = Probe(name="triage", persistence_dir=str(tmp_path))
        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
            response_metadata={"model_name": "gpt-4o-mini"},
        )
        result = LLMResult(generations=[[ChatGeneration(message=message)]])

        assert token_usage(result) == (1000, 500, "gpt-4o-mini")
        assert cost_of({"gpt-4o-mini": (0.15, 0.60)}, "gpt-4o-mini", 1000, 500) == pytest.approx(
            0.00045
        )
        CostCallback(actor, prices={"gpt-4o-mini": (0.15, 0.60)}).on_llm_end(result, run_id=None)

        assert actor.metrics.llm_calls == 1
        assert (actor.metrics.llm_input_tokens, actor.metrics.llm_output_tokens) == (1000, 500)
        assert actor.metrics.llm_cost_usd == pytest.approx(0.00045)
        assert ledger == [pytest.approx(0.00045)]

    def test_an_unpriced_model_counts_tokens_at_no_cost(
        self, tmp_path: Path, ledger: list[float]
    ) -> None:
        pytest.importorskip("langchain_core")
        from langchain_core.outputs import Generation, LLMResult

        from wactorz.core.integrations.langchain import CostCallback

        actor = Probe(name="triage", persistence_dir=str(tmp_path))
        # The older shape: a plain generation, usage in llm_output.
        result = LLMResult(
            generations=[[Generation(text="ok")]],
            llm_output={
                "token_usage": {"prompt_tokens": 7, "completion_tokens": 3},
                "model_name": "local",
            },
        )
        CostCallback(actor).on_llm_end(result, run_id=None)

        assert actor.metrics.llm_input_tokens == 7 and actor.metrics.llm_output_tokens == 3
        assert actor.metrics.llm_cost_usd == 0.0


class TestAG2Usage:
    def test_a_chat_is_reported_once_as_the_difference_since_the_last_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("autogen")
        from wactorz.core.integrations import ag2 as ag2_ext

        monkeypatch.setattr(cost_module, "accumulate_global_cost", lambda delta: None)
        totals = {"total_cost": 0.002, "gpt-4o": {"prompt_tokens": 100, "completion_tokens": 40}}
        monkeypatch.setattr(
            ag2_ext,
            "gather_usage_summary",
            lambda agents: {"usage_including_cached_inference": dict(totals)},
        )
        actor = Probe(name="review", persistence_dir=str(tmp_path))

        assert ag2_ext.record_usage(actor, [object()]) == pytest.approx(0.002)
        assert ag2_ext.record_usage(actor, [object()]) == 0.0, "nothing new since"
        totals.update(
            {"total_cost": 0.005, "gpt-4o": {"prompt_tokens": 250, "completion_tokens": 90}}
        )
        assert ag2_ext.record_usage(actor, [object()]) == pytest.approx(0.003)

        assert actor.metrics.llm_calls == 2
        assert actor.metrics.llm_cost_usd == pytest.approx(0.005)
        assert (actor.metrics.llm_input_tokens, actor.metrics.llm_output_tokens) == (250, 90)
