"""The planner around its plans: routing, workers, caching, and ending.

A planner is spawned for one request and must not outlive it. Every way it
ends — finishing, hitting its lifetime cap, or being told to stop — goes
through one idempotent teardown that fails its pending delegations, lets go of
the supervisor and registry, and withdraws its card.

Which path a request takes is decided before any model is asked: an approved
plan, a reactive request and a dry run all go to the pipeline planner, and only
a plain one-shot task is decomposed here, with the result cached against the
workers that were alive to run it.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.llm.providers.fake import FakeProvider
from wactorz.agents.planner import agent as agent_mod
from wactorz.agents.planner.agent import PlannerAgent, _fmt_worker
from wactorz.agents.planner.cache import PLAN_CACHE_KEY
from wactorz.core import topic_bus
from wactorz.core.actor import Message, MessageType
from wactorz.core.topic_bus import TopicBus, TopicContract


class _Actor:
    def __init__(self, name: str, **attrs: Any) -> None:
        self.name = name
        self.actor_id = f"id-{name}"
        for key, value in attrs.items():
            setattr(self, key, value)


class _Supervisor:
    def __init__(self, fail: bool = False) -> None:
        self.released: list[str] = []
        self._fail = fail

    def release(self, name: str) -> None:
        if self._fail:
            raise RuntimeError("supervisor busy")
        self.released.append(name)


class _Registry:
    def __init__(self, *actors: Any, supervisor: Any = None, fail: bool = False) -> None:
        self._actors = list(actors)
        self._supervisor_ref = supervisor
        self._fail = fail
        self.unregistered: list[str] = []

    def find_by_name(self, name: str) -> Any:
        return next((a for a in self._actors if a.name == name), None)

    def all_actors(self) -> list[Any]:
        return list(self._actors)

    async def unregister(self, actor_id: str) -> None:
        if self._fail:
            raise RuntimeError("registry locked")
        self.unregistered.append(actor_id)


class _Main:
    def __init__(
        self,
        known_nodes: dict[str, dict[str, Any]] | None = None,
        capabilities: list[dict[str, Any]] | None = None,
        facts: dict[str, Any] | Exception | None = None,
    ) -> None:
        self._known_nodes = known_nodes or {}
        self._capabilities = capabilities or []
        self._facts = facts

    def list_capabilities(self) -> list[dict[str, Any]]:
        return self._capabilities

    def get_user_facts(self) -> dict[str, Any]:
        if isinstance(self._facts, Exception):
            raise self._facts
        return self._facts or {}


class _Llm:
    """A provider that raises from `complete`, without the retry wrapper."""

    async def complete(self, **_kwargs: Any) -> Any:
        raise RuntimeError("model overloaded")


def _planner(tmp_path: Path, script: dict[str, str] | None = None, **kwargs: Any) -> PlannerAgent:
    return PlannerAgent(
        llm_provider=FakeProvider(script=script or {}),
        persistence_dir=str(tmp_path),
        auto_terminate=False,
        **kwargs,
    )


@pytest.fixture(name="planner")
def planner_fixture(tmp_path: Path) -> PlannerAgent:
    return _planner(tmp_path)


def _use_main(monkeypatch: pytest.MonkeyPatch, main: _Main | None) -> None:
    monkeypatch.setattr(agent_mod, "find_main_actor", lambda _registry: main)


def _record_logs(planner: PlannerAgent) -> list[str]:
    logged: list[str] = []

    async def _log(message: str) -> None:
        logged.append(message)

    planner._log = _log  # pyright: ignore[reportAttributeAccessIssue]
    return logged


async def _finish(*tasks: "asyncio.Task[Any] | None") -> None:
    pending = {t for t in tasks if t is not None}
    for task in pending:
        task.cancel()
    if pending:
        # `asyncio.wait`, not `wait_for`, which can lose a cancellation on 3.10/3.11.
        await asyncio.wait(pending, timeout=5)


class TestStart:
    async def test_an_approved_plan_overrides_a_dry_run(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, plan_only=True, approved_plan={"plan": []})
        logged = _record_logs(planner)

        await planner.on_start()
        await _finish(planner._lifetime_task)

        assert planner._plan_only is False
        assert any("approved_plan takes precedence" in line for line in logged)

    async def test_a_task_given_at_spawn_is_planned_and_reported(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, task="summarise the news", reply_to_id="main-id")
        sent: list[Any] = []

        async def _run(task: str) -> str:
            return f"done: {task}"

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, payload))
            return True

        planner._run_plan = _run  # pyright: ignore[reportAttributeAccessIssue]
        planner.send = _send  # pyright: ignore[reportAttributeAccessIssue]

        await planner.on_start()
        for _ in range(10):
            await asyncio.sleep(0)
        await _finish(planner._lifetime_task)

        assert sent == [
            (
                "main-id",
                {
                    "result": "done: summarise the news",
                    "text": "done: summarise the news",
                    "spawn_results": {},
                },
            )
        ]


class TestReportPlan:
    async def test_the_report_carries_the_task_id_and_what_was_spawned(
        self, tmp_path: Path
    ) -> None:
        planner = _planner(tmp_path, reply_to_id="main-id", reply_task_id="t9")
        planner._spawned_by_planner = ["watcher"]
        planner._spawn_results = {"watcher": {"ok": True}}
        sent: list[Any] = []

        async def _run(task: str) -> str:
            return "ok"

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append(payload)
            return True

        planner._run_plan = _run  # pyright: ignore[reportAttributeAccessIssue]
        planner.send = _send  # pyright: ignore[reportAttributeAccessIssue]

        await planner._report_plan("t")

        assert sent[0]["_task_id"] == "t9"
        assert sent[0]["spawned"] == ["watcher"]
        assert sent[0]["spawn_results"] == {"watcher": {"ok": True}}

    async def test_without_anyone_to_report_to_nothing_is_sent(self, planner: PlannerAgent) -> None:
        sent: list[Any] = []

        async def _run(task: str) -> str:
            return "ok"

        planner._run_plan = _run  # pyright: ignore[reportAttributeAccessIssue]
        planner.send = lambda *a, **k: sent.append(a)  # pyright: ignore[reportAttributeAccessIssue]

        await planner._report_plan("t")

        assert sent == []


class TestStopAndMetrics:
    async def test_spend_is_persisted_so_it_outlives_the_planner(
        self, planner: PlannerAgent
    ) -> None:
        planner._accrue_usage({"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.25})

        await planner.on_stop()

        final = planner.recall("_final_cost")
        assert final["cost_usd"] == 0.25
        assert final["input_tokens"] == 10
        metrics = planner._build_metrics()
        assert (metrics["input_tokens"], metrics["output_tokens"]) == (10, 5)

    async def test_nothing_spent_persists_nothing(self, planner: PlannerAgent) -> None:
        await planner.on_stop()

        assert planner.recall("_final_cost") is None

    async def test_a_failed_metrics_publish_does_not_fail_the_stop(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _broken(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("broker gone")

        monkeypatch.setattr(planner, "_mqtt_publish", _broken)

        await planner.on_stop()

    def test_a_usage_with_no_cost_does_not_move_the_period(self, planner: PlannerAgent) -> None:
        planner._accrue_usage({"input_tokens": 3})

        assert planner._last_period_cost_usd == 0.0


class TestNowContext:
    def test_the_users_timezone_comes_from_main(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(monkeypatch, _Main(facts={"pref_timezone": "Asia/Tokyo"}))

        assert "(UTC+0900)" in planner._now_context()

    def test_unreadable_facts_fall_back_to_the_default(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(monkeypatch, _Main(facts=RuntimeError("db locked")))

        assert planner._now_context()


class TestHandleMessage:
    async def test_a_task_is_planned_and_answered_to_whoever_asked(
        self, planner: PlannerAgent
    ) -> None:
        sent: list[Any] = []
        planned: list[str] = []

        async def _run(task: str) -> str:
            planned.append(task)
            return "answer"

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            sent.append((target, msg_type, payload))
            return True

        planner._run_plan = _run  # pyright: ignore[reportAttributeAccessIssue]
        planner.send = _send  # pyright: ignore[reportAttributeAccessIssue]
        planner._spawned_by_planner = ["a"]

        await planner.handle_message(
            Message(
                type=MessageType.TASK,
                sender_id="sender",
                payload={"task": "do it", "_reply_to": "asker", "_task_id": "t1"},
            )
        )

        assert planned == ["do it"]
        assert sent == [
            (
                "asker",
                MessageType.RESULT,
                {"result": "answer", "text": "answer", "_task_id": "t1", "spawned": ["a"]},
            )
        ]

    async def test_a_text_payload_is_the_task(self, planner: PlannerAgent) -> None:
        planned: list[str] = []

        async def _run(task: str) -> str:
            planned.append(task)
            return "answer"

        planner._run_plan = _run  # pyright: ignore[reportAttributeAccessIssue]
        planner.send = lambda *a, **k: asyncio.sleep(0)  # pyright: ignore[reportAttributeAccessIssue]

        await planner.handle_message(Message(type=MessageType.TASK, sender_id="s", payload="hi"))
        await planner.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="s"))

        assert planned == ["hi"]


class TestRunPlanRouting:
    @staticmethod
    def _routes(planner: PlannerAgent) -> list[str]:
        taken: list[str] = []

        async def _pipeline(task: str, workers: list[dict[str, Any]]) -> str:
            taken.append("pipeline")
            return "pipeline"

        async def _answer(task: str) -> str:
            taken.append("answer")
            return "answer"

        async def _prune() -> None:
            return None

        planner._run_pipeline = _pipeline  # pyright: ignore[reportAttributeAccessIssue]
        planner._llm_answer = _answer  # pyright: ignore[reportAttributeAccessIssue]
        planner._prune_stale_contracts = _prune  # pyright: ignore[reportAttributeAccessIssue]
        return taken

    async def test_an_approved_plan_goes_to_the_pipeline(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, approved_plan={"plan": [{"name": "a"}]})
        taken = self._routes(planner)

        await planner._run_plan("anything")

        assert taken == ["pipeline"]

    async def test_a_reactive_request_goes_to_the_pipeline(self, planner: PlannerAgent) -> None:
        taken = self._routes(planner)

        await planner._run_plan("pipeline: when the door opens notify me")

        assert taken == ["pipeline"]

    async def test_a_dry_run_of_a_one_shot_task_goes_to_the_pipeline(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, plan_only=True)
        taken = self._routes(planner)

        await planner._run_plan("summarise the news")

        assert taken == ["pipeline"]

    async def test_a_task_that_cannot_be_decomposed_is_answered_directly(
        self, planner: PlannerAgent
    ) -> None:
        taken = self._routes(planner)

        async def _decompose(task: str, workers: list[dict[str, Any]]) -> list[Any]:
            return []

        planner._decompose = _decompose  # pyright: ignore[reportAttributeAccessIssue]

        assert await planner._run_plan("summarise the news") == "answer"
        assert taken == ["answer"]


class TestRunPlanOneShot:
    PLAN: list[dict[str, Any]] = [{"step": 1, "agent": "news", "task": "get news"}]  # noqa: RUF012  # read-only fixture

    @staticmethod
    def _executes(planner: PlannerAgent, plan: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"decompose": 0, "ensure": 0, "execute": 0, "stop": 0}

        async def _prune() -> None:
            return None

        async def _decompose(task: str, workers: list[dict[str, Any]]) -> list[dict[str, Any]]:
            counts["decompose"] += 1
            return plan

        async def _ensure(p: list[dict[str, Any]]) -> list[dict[str, Any]]:
            counts["ensure"] += 1
            return p

        async def _execute(p: list[dict[str, Any]]) -> dict[int, Any]:
            counts["execute"] += 1
            return {1: {"result": "headlines"}}

        async def _synthesize(task: str, p: list[dict[str, Any]], results: Any) -> str:
            return f"synthesised {results[1]['result']}"

        async def _stop(delay: float = 2.0) -> None:
            counts["stop"] += 1

        planner._prune_stale_contracts = _prune  # pyright: ignore[reportAttributeAccessIssue]
        planner._decompose = _decompose  # pyright: ignore[reportAttributeAccessIssue]
        planner._ensure_agents = _ensure  # pyright: ignore[reportAttributeAccessIssue]
        planner._execute = _execute  # pyright: ignore[reportAttributeAccessIssue]
        planner._synthesize = _synthesize  # pyright: ignore[reportAttributeAccessIssue]
        planner._deferred_stop = _stop  # pyright: ignore[reportAttributeAccessIssue]
        return counts

    async def test_the_plan_is_executed_synthesised_and_cached(self, planner: PlannerAgent) -> None:
        planner._registry = _Registry(_Actor("news"))  # pyright: ignore[reportAttributeAccessIssue]
        counts = self._executes(planner, self.PLAN)

        first = await planner._run_plan("summarise the news")
        second = await planner._run_plan("summarise the news")

        assert first == second == "synthesised headlines"
        assert counts["decompose"] == 1, "the second run must come from the cache"
        assert counts["execute"] == 2
        assert planner.recall(PLAN_CACHE_KEY)

    async def test_a_cached_plan_whose_agent_is_gone_is_planned_again(
        self, planner: PlannerAgent
    ) -> None:
        planner._registry = _Registry(_Actor("news"))  # pyright: ignore[reportAttributeAccessIssue]
        counts = self._executes(planner, self.PLAN)
        await planner._run_plan("summarise the news")

        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        await planner._run_plan("summarise the news")

        assert counts["decompose"] == 2

    async def test_a_self_terminating_planner_schedules_its_stop(self, tmp_path: Path) -> None:
        planner = PlannerAgent(llm_provider=FakeProvider(), persistence_dir=str(tmp_path))
        counts = self._executes(planner, self.PLAN)

        await planner._run_plan("summarise the news")
        await asyncio.sleep(0)

        assert counts["stop"] == 1


class TestWorkerDiscovery:
    def test_without_a_registry_there_are_no_workers(self, planner: PlannerAgent) -> None:
        assert planner._discover_workers() == []

    def test_local_workers_skip_system_agents_and_other_planners(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner._registry = _Registry(  # pyright: ignore[reportAttributeAccessIssue]
            _Actor("main"),
            _Actor("planner-7"),
            planner,
            _Actor("news", description="fetches news"),
            _Actor("chatty", system_prompt="You are a very chatty assistant " * 10),
            _Actor("bare"),
        )
        _use_main(
            monkeypatch,
            _Main(
                capabilities=[{"name": "news", "description": "declared", "capabilities": ["n"]}]
            ),
        )

        workers = {w["name"]: w for w in planner._discover_workers()}

        assert set(workers) == {"news", "chatty", "bare"}
        assert workers["news"]["description"] == "declared"
        assert workers["news"]["capabilities"] == ["n"]
        assert len(workers["chatty"]["description"]) == 100
        assert workers["bare"]["description"] == "_Actor"

    def test_remote_workers_come_from_nodes_seen_recently(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = time.time()
        planner._registry = _Registry(_Actor("news"))  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(
            monkeypatch,
            _Main(
                known_nodes={
                    "rpi": {"last_seen": now, "agents": ["camera", "news", "monitor"]},
                    "gone": {"last_seen": now - 600, "agents": ["old-sensor"]},
                },
                capabilities=[{"name": "camera", "description": "takes photos"}],
            ),
        )

        workers = {w["name"]: w for w in planner._discover_workers()}

        assert set(workers) == {"news", "camera"}
        assert workers["camera"]["remote"] is True
        assert workers["camera"]["node"] == "rpi"
        assert workers["camera"]["description"] == "takes photos"


class TestPruneStaleContracts:
    async def test_contracts_of_agents_no_longer_running_are_dropped(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        for name in ("local", "remote", "dead", "offline-remote"):
            bus.register_contract(TopicContract(name=name, publishes=[f"{name}/x"]))
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        now = time.time()
        planner._registry = _Registry(_Actor("local"))  # pyright: ignore[reportAttributeAccessIssue]
        _use_main(
            monkeypatch,
            _Main(
                known_nodes={
                    "rpi": {"last_seen": now, "agents": ["remote"]},
                    "old": {"last_seen": now - 600, "agents": ["offline-remote"]},
                }
            ),
        )
        logged = _record_logs(planner)

        await planner._prune_stale_contracts()

        assert {c.name for c in bus.registry.all_contracts()} == {"local", "remote"}
        assert logged and logged[0].startswith("Pruned 2 stale TopicBus contract(s)")

    async def test_a_broken_bus_is_skipped(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken() -> TopicBus:
            raise RuntimeError("no bus")

        monkeypatch.setattr(topic_bus, "get_topic_bus", _broken)
        planner._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]

        await planner._prune_stale_contracts()


class TestDecompose:
    async def test_without_an_llm_there_is_no_plan(self, tmp_path: Path) -> None:
        planner = PlannerAgent(llm_provider=None, persistence_dir=str(tmp_path))

        assert await planner._decompose("t", []) == []
        assert await planner._llm_answer("hello") == "[No LLM available: hello]"

    async def test_the_workers_and_topic_samples_are_in_the_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        planner = _planner(tmp_path, script={"summarise": '[{"step": 1, "agent": "news"}]'})
        bus = TopicBus()
        contract = TopicContract(name="thermo", publishes=["sensors/t"])
        contract.update_observed("sensors/t", {"temp": 20.5})
        bus.register_contract(contract)
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        workers = [{"name": "news", "type": "LLMAgent", "description": "fetches news"}]

        plan = await planner._decompose("summarise the news", workers)

        assert plan == [{"step": 1, "agent": "news"}]
        assert isinstance(planner.llm, FakeProvider)
        prompt = planner.llm.calls[-1][1][-1]["content"]
        assert "news (LLMAgent): fetches news" in prompt
        assert "sensors/t (by thermo)" in prompt

    @pytest.mark.parametrize("answer", ["[]", "no plan today"])
    async def test_an_empty_or_unreadable_answer_is_no_plan(
        self, tmp_path: Path, answer: str
    ) -> None:
        planner = _planner(tmp_path, script={"summarise": answer})

        assert await planner._decompose("summarise the news", []) == []

    async def test_a_provider_that_fails_is_no_plan_and_an_error_answer(
        self, planner: PlannerAgent
    ) -> None:
        planner.llm = _Llm()  # pyright: ignore[reportAttributeAccessIssue]

        assert await planner._decompose("t", []) == []
        assert await planner._llm_answer("t") == "[LLM error: model overloaded]"

    async def test_a_direct_answer_counts_its_spend(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, script={"hello": "hi there"})

        assert await planner._llm_answer("hello") == "hi there"
        assert planner.total_cost_usd > 0


class TestTopicSchemaContext:
    async def test_without_samples_live_topics_are_sampled(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bus = TopicBus()
        bus.register_contract(TopicContract(name="thermo", publishes=["sensors/t"]))
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)

        async def _sample(_bus: Any) -> list[str]:
            return ["  sensors/t live"]

        planner._sample_live_topics = _sample  # pyright: ignore[reportAttributeAccessIssue]

        assert "  sensors/t live" in await planner._topic_schema_context()

    async def test_no_bus_means_no_context(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_bus, "_topic_bus", None)

        assert await planner._topic_schema_context() == ""

    async def test_a_failing_sampler_means_no_context(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(topic_bus, "_topic_bus", TopicBus())

        async def _broken(_bus: Any) -> list[str]:
            raise OSError("broker down")

        planner._sample_live_topics = _broken  # pyright: ignore[reportAttributeAccessIssue]

        assert await planner._topic_schema_context() == ""


class TestWorkerFormatting:
    def test_every_declared_schema_is_listed(self) -> None:
        line = _fmt_worker(
            {
                "name": "cam",
                "type": "RemoteAgent",
                "remote": True,
                "node": "rpi",
                "description": "camera",
                "capabilities": ["photo", "video"],
                "input_schema": {"q": "str"},
                "output_schema": {"url": "str"},
                "publishes": ["cam/frames"],
                "observed_samples": {"cam/frames": {"fields": {"n": "int"}, "example": {"n": 1}}},
            }
        )

        assert line.splitlines() == [
            "  - cam (RemoteAgent on rpi): camera",
            "    capabilities: photo, video",
            "    input_schema : {'q': 'str'}",
            "    output_schema: {'url': 'str'}",
            "    publishes: ['cam/frames']",
            "    topic 'cam/frames' payload fields: {'n': 'int'}  example: {'n': 1}",
        ]


class TestEnding:
    async def test_the_lifetime_cap_terminates_the_planner(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, max_lifetime_s=0)
        terminated: list[bool] = []

        async def _terminate() -> None:
            terminated.append(True)

        planner._terminate = _terminate  # pyright: ignore[reportAttributeAccessIssue]

        await planner._lifetime_watchdog()

        assert terminated == [True]

    async def test_a_cancelled_watchdog_ends_quietly(self, tmp_path: Path) -> None:
        planner = _planner(tmp_path, max_lifetime_s=60)
        task = asyncio.create_task(planner._lifetime_watchdog())
        await asyncio.sleep(0)

        await _finish(task)

        assert task.done()
        assert not planner._terminated

    async def test_teardown_fails_pending_work_releases_and_withdraws_once(
        self, planner: PlannerAgent, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        supervisor = _Supervisor()
        registry = _Registry(supervisor=supervisor)
        planner._registry = registry  # pyright: ignore[reportAttributeAccessIssue]
        pending: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        planner._result_futures["t1"] = pending
        planner._lifetime_task = asyncio.create_task(asyncio.sleep(60))
        published: list[tuple[str, Any]] = []

        async def _publish(topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
            published.append((topic, payload))

        async def _stop() -> None:
            raise RuntimeError("already stopping")

        monkeypatch.setattr(planner, "_mqtt_publish", _publish)
        monkeypatch.setattr(planner, "stop", _stop)

        await planner._terminate()
        await planner._terminate()
        await asyncio.sleep(0)

        assert pending.cancelled()
        assert planner._result_futures == {}
        assert planner._lifetime_task.cancelled()
        assert supervisor.released == [planner.name]
        assert registry.unregistered == [planner.actor_id]
        assert published.count((f"agents/{planner.actor_id}/manifest", b"")) == 1

    async def test_release_survives_a_failing_supervisor_and_registry(
        self, planner: PlannerAgent
    ) -> None:
        planner._registry = _Registry(supervisor=_Supervisor(fail=True), fail=True)  # pyright: ignore[reportAttributeAccessIssue]

        await planner._release_from_registry()

    async def test_a_deferred_stop_terminates_after_the_delay(self, planner: PlannerAgent) -> None:
        terminated: list[bool] = []

        async def _terminate() -> None:
            terminated.append(True)

        planner._terminate = _terminate  # pyright: ignore[reportAttributeAccessIssue]

        await planner._deferred_stop(delay=0)

        assert terminated == [True]

    def test_the_task_description_is_the_task(self, tmp_path: Path) -> None:
        assert _planner(tmp_path)._current_task_description() == "waiting for task"
        assert _planner(tmp_path, task="x" * 100)._current_task_description() == "x" * 60
