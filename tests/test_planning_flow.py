"""The dry-run flow end to end: plan, propose, approve, revise, reject, run.

`tests/test_planning.py` covers the predicates and the persistence. This drives
the orchestration around them with a planner that answers at once, so what is
pinned is the conversation: a pipeline request is proposed rather than built,
"yes" builds exactly the plan that was shown, a correction replaces the plan
rather than stacking a second one beside it, and a bypass marker skips the
approval step without leaking into the task the planner sees.

`_run_planner` waits for the planner's reply no longer than the planner can
live, and a vague follow-up is given the recent conversation while a pipeline
declaration deliberately is not.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.main import planning
from wactorz.agents.main.planning import PENDING_PLANS_KEY, PlanningMixin
from wactorz.agents.planner import PlannerAgent

ENVELOPE = {
    "_plan_proposal": True,
    "task": "when the door opens notify me",
    "plan": [{"name": "door-watcher", "spawn_config": {"type": "dynamic", "code": "pass"}}],
}


class _Actor:
    def __init__(self, name: str) -> None:
        self.name = name
        self.actor_id = f"id-{name}"
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _Registry:
    def __init__(self, *actors: _Actor) -> None:
        self._actors = list(actors)
        self.unregistered: list[str] = []

    def find_by_name(self, name: str) -> _Actor | None:
        return next((a for a in self._actors if a.name == name), None)

    def all_actors(self) -> list[_Actor]:
        return list(self._actors)

    async def unregister(self, actor_id: str) -> None:
        self.unregistered.append(actor_id)


class _Host(PlanningMixin):
    """Main, as far as planning reaches into it, with a planner that answers at once."""

    def __init__(self, registry: _Registry | None = None) -> None:
        self.name = "main"
        self.actor_id = "main-id"
        self.llm = object()  # pyright: ignore[reportAttributeAccessIssue]
        self._store: dict[str, Any] = {}
        self._registry = registry  # pyright: ignore[reportAttributeAccessIssue]
        self._facts: dict[str, Any] = {}
        # Only its parent is read, as the directory a planner persists under.
        self._persistence_dir = Path("state") / "main"
        self._result_futures: dict[str, asyncio.Future[Any]] = {}
        self._conversation_history: list[dict[str, Any]] = []
        self.published: list[Any] = []
        self.spawned: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.cleared: list[str] = []
        self.recorded: list[tuple[str, str]] = []
        #: What the next planner replies with: a payload, None for "never", or
        #: an exception raised from spawn.
        self.replies: list[Any] = []

    def recall(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def persist(self, key: str, value: Any) -> None:
        self._store[key] = value

    def get_user_facts(self) -> dict[str, Any]:
        return self._facts

    async def _mqtt_publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> None:
        self.published.append((topic, payload))

    async def spawn(self, actor_class: type, **kwargs: Any) -> Any:
        self.spawned.append(kwargs)
        reply = self.replies.pop(0) if self.replies else {"result": "done"}
        if isinstance(reply, Exception):
            raise reply
        if reply == "no planner":
            return None
        if reply is not None:
            self._result_futures[kwargs["reply_task_id"]].set_result(reply)
        return object()

    def _remove_from_spawn_registry(self, name: str) -> None:
        self.removed.append(name)

    async def _clear_agent_manifest(self, name: str, actor_id: str) -> None:
        self.cleared.append(name)

    def _record_agent_deletion(self, name: str, reason: str = "") -> None:
        self.recorded.append((name, reason))


def _host(registry: _Registry | None = None) -> _Host:
    # Partial on purpose: the members planning reaches, not the whole MainActor.
    return _Host(registry)  # pyright: ignore[reportAbstractUsage]


def _proposal(result: str = "") -> dict[str, Any]:
    return {"result": result or json.dumps(ENVELOPE)}


def _pending(host: _Host) -> list[dict[str, Any]]:
    return [p for p in host._store.get(PENDING_PLANS_KEY, {}).values() if p["status"] == "pending"]


class TestRunPlanner:
    async def test_the_planner_is_spawned_with_the_task_and_its_answer_returned(self) -> None:
        host = _host()
        host.replies = [{"result": "done", "spawned": ["door-watcher"]}]

        answer = await host._run_planner("when the door opens notify me", is_pipeline_intent=True)

        assert answer == (
            "done\n\n[System: Planner created new agents: door-watcher — saved for future use]"
        )
        (kwargs,) = host.spawned
        assert kwargs["task"] == "when the door opens notify me"
        assert kwargs["name"].startswith("planner-")
        assert kwargs["max_lifetime_s"] == PlannerAgent.DEFAULT_MAX_LIFETIME_S
        assert kwargs["plan_only"] is False
        assert host._result_futures == {}
        assert "plan-and-execute" in host.published[0][1]["message"]

    async def test_a_short_follow_up_is_given_the_recent_conversation(self) -> None:
        host = _host()
        host._conversation_history = [
            {"role": "user", "content": "find the living room sensor"},
            {"role": "assistant", "content": "sensor.living_temp"},
        ]

        await host._run_planner("use it")

        task = host.spawned[0]["task"]
        assert task.startswith("use it\n\n[Context from recent conversation:]")
        assert "User: find the living room sensor" in task
        assert "Assistant: sensor.living_temp" in task

    @pytest.mark.parametrize("kwargs", [{"is_pipeline_intent": True}, {"approved_plan": ENVELOPE}])
    async def test_pipelines_and_approved_plans_get_no_conversation(
        self, kwargs: dict[str, Any]
    ) -> None:
        host = _host()
        host._conversation_history = [{"role": "user", "content": "door stuff"}]

        await host._run_planner("use it", **kwargs)

        assert host.spawned[0]["task"] == "use it"

    async def test_the_text_field_is_accepted_as_the_answer(self) -> None:
        host = _host()
        host.replies = [{"text": "from text"}]

        assert await host._run_planner("t") == "from text"

    async def test_a_planner_that_never_starts_gives_no_answer(self) -> None:
        host = _host()
        host.replies = ["no planner"]

        assert await host._run_planner("t") is None

    async def test_a_spawn_error_gives_no_answer(self) -> None:
        host = _host()
        host.replies = [RuntimeError("registry full")]

        assert await host._run_planner("t") is None
        assert host._result_futures == {}

    async def test_a_planner_that_outlives_its_cap_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(PlannerAgent, "DEFAULT_MAX_LIFETIME_S", 0.0)
        monkeypatch.setattr(planning, "PLANNER_REPLY_GRACE_S", 0.01)
        host = _host()
        host.replies = [None]

        answer = await host._run_planner("t")

        assert answer is not None and answer.startswith("The pipeline is taking longer")
        assert host._result_futures == {}


class TestProposeOrExecute:
    async def test_a_pipeline_request_is_proposed_not_built(self) -> None:
        host = _host()
        host.replies = [_proposal()]

        reply = await host._propose_or_execute_pipeline("when the door opens notify me")

        assert host.spawned[0]["plan_only"] is True
        (plan,) = _pending(host)
        assert plan["envelope"] == ENVELOPE
        assert reply.startswith(f"**Proposed pipeline** (id `{plan['plan_id']}`)")

    async def test_a_bypass_marker_builds_at_once_without_the_marker(self) -> None:
        host = _host()

        reply = await host._propose_or_execute_pipeline("pipeline! when the door opens notify me")

        assert reply == "done"
        assert host.spawned[0]["task"] == "when the door opens notify me"
        assert host.spawned[0]["plan_only"] is False

    async def test_a_bypass_with_no_answer_asks_for_a_retry(self) -> None:
        host = _host()
        host.replies = [RuntimeError("down")]

        reply = await host._propose_or_execute_pipeline("pipeline! when x")

        assert reply == "Planner did not return a result. Please retry."

    async def test_no_answer_asks_for_a_retry(self) -> None:
        host = _host()
        host.replies = [RuntimeError("down")]

        assert (
            await host._propose_or_execute_pipeline("when x")
            == "Planner did not return a result. Please retry."
        )

    async def test_an_answer_that_is_not_a_proposal_is_passed_through(self) -> None:
        host = _host()
        host.replies = [{"result": "Cannot set up this pipeline: no door sensor"}]

        reply = await host._propose_or_execute_pipeline("when the door opens notify me")

        assert reply == "Cannot set up this pipeline: no door sensor"
        assert _pending(host) == []


class TestRespondingToAProposal:
    @staticmethod
    async def _proposed() -> tuple[_Host, dict[str, Any]]:
        host = _host()
        host.replies = [_proposal()]
        await host._propose_or_execute_pipeline("when the door opens notify me")
        (plan,) = _pending(host)
        return host, plan

    async def test_nothing_pending_is_not_a_response(self) -> None:
        assert await _host()._handle_pending_plan_response("yes") is None

    async def test_yes_builds_exactly_the_plan_that_was_shown(self) -> None:
        host, plan = await self._proposed()

        reply = await host._handle_pending_plan_response("Yes please!")

        assert reply == f"✅ Approved plan `{plan['plan_id']}`. done"
        assert host.spawned[-1]["approved_plan"] == ENVELOPE
        assert _pending(host) == []

    async def test_an_approval_with_no_answer_still_confirms(self) -> None:
        host, plan = await self._proposed()
        host.replies = [RuntimeError("down")]

        reply = await host._handle_pending_plan_response("ok")

        assert reply == f"✅ Approved plan `{plan['plan_id']}`. Spawn complete."

    async def test_no_discards_the_plan(self) -> None:
        host, plan = await self._proposed()

        reply = await host._handle_pending_plan_response("no thanks")

        assert reply is not None and reply.startswith(f"❌ Discarded plan `{plan['plan_id']}`")
        assert host._store[PENDING_PLANS_KEY][plan["plan_id"]]["status"] == "rejected"

    async def test_a_correction_replaces_the_plan(self) -> None:
        host, plan = await self._proposed()
        host.replies = [_proposal()]

        reply = await host._handle_pending_plan_response("actually make the threshold 55%")

        (revised,) = _pending(host)
        assert revised["supersedes"] == plan["plan_id"]
        assert revised["task"] == "when the door opens notify me"
        assert (
            "[User correction to the previous plan: actually make the threshold 55%]"
            in (host.spawned[-1]["task"])
        )
        assert reply is not None
        assert reply.startswith(f"📝 Got it — revising plan `{plan['plan_id']}`")
        assert host._store[PENDING_PLANS_KEY][plan["plan_id"]]["status"] == "superseded"

    async def test_a_correction_the_planner_cannot_answer_is_reported(self) -> None:
        host, plan = await self._proposed()
        host.replies = [RuntimeError("down")]

        reply = await host._handle_pending_plan_response("use 30 seconds instead")

        assert reply == (
            f"Could not revise plan `{plan['plan_id']}`. The planner did not respond — please retry."
        )

    async def test_a_correction_answered_in_prose_is_passed_through(self) -> None:
        host, _ = await self._proposed()
        host.replies = [{"result": "That sensor does not exist."}]

        assert (
            await host._handle_pending_plan_response("use the bedroom sensor instead")
            == "That sensor does not exist."
        )

    @pytest.mark.parametrize("text", ["what time is it", "   "])
    async def test_anything_else_leaves_the_plan_pending(self, text: str) -> None:
        host, _ = await self._proposed()

        assert await host._handle_pending_plan_response(text) is None
        assert len(_pending(host)) == 1


class TestFormatting:
    def test_a_scheduled_agent_shows_when_it_fires_and_where_it_publishes(self) -> None:
        host = _host()
        host._facts = {"pref_timezone": "Europe/Athens"}
        plan = {
            "plan_id": "p1",
            "task": "lights at five",
            "envelope": {
                "agents": [
                    {
                        "name": "five-pm",
                        "spawn_config": {
                            "type": "scheduled",
                            "schedule": {"type": "daily", "at": "17:00"},
                        },
                    },
                    {
                        "name": "notifier",
                        "publishes": ["alerts/out"],
                        "spawn_config": {
                            "type": "dynamic",
                            "mqtt_topics": ["schedule/five-pm/fired"],
                            "install": "httpx",
                            "code": "post to https://discord.com/api/webhooks/1 and api.telegram.org "
                            "homeassistant call_service",
                        },
                    },
                ],
                "warnings": "May contradict rule [r1]",
            },
        }

        text = host._format_plan_proposal(plan)

        assert "     fires: every day at 17:00 (Europe/Athens)" in text
        assert "     publishes: schedule/five-pm/fired" in text
        assert "     listens on: schedule/five-pm/fired" in text
        assert "     publishes: alerts/out" in text
        assert "posts to Discord" in text and "posts to Telegram" in text
        assert "controls Home Assistant device" in text
        assert "     installs: httpx" in text
        assert "⚠️ **Heads up — possible overlap with existing rules**" in text

    def test_an_unrenderable_schedule_is_shown_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        host = _host()

        def _broken() -> dict[str, Any]:
            raise RuntimeError("facts unavailable")

        monkeypatch.setattr(host, "get_user_facts", _broken)
        plan = {
            "plan_id": "p1",
            "envelope": {
                "plan": [
                    {
                        "name": "tick",
                        "spawn_config": {
                            "type": "scheduled",
                            "schedule": {"type": "interval", "seconds": 5},
                            "publish_topic": "tick/out",
                        },
                    }
                ]
            },
        }

        text = host._format_plan_proposal(plan)

        assert "     fires: {'type': 'interval', 'seconds': 5}" in text
        assert "     publishes: tick/out" in text

    def test_ha_actions_and_guards_are_described(self) -> None:
        lines = planning._describe_ha_actions(
            [
                "not a dict",
                {"service": "turn_on"},
                {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": "light.hall",
                    "service_data": {"brightness": 40},
                },
            ]
        )
        guard = planning._describe_ha_guards(
            {
                "detection_filter": {"person": True},
                "conditions": ["junk", {"entity_id": "sun.sun", "operator": "weird", "value": 1}],
            }
        )

        assert lines == [
            "calls a Home Assistant service (malformed action — check the full plan)",
            "calls Home Assistant turn_on",
            "calls Home Assistant light.turn_on on light.hall with brightness=40",
        ]
        assert guard == "the trigger has person=True and sun.sun state weird 1"


class TestDeletingARule:
    async def test_an_unknown_rule_is_reported(self) -> None:
        assert await _host().delete_pipeline_rule("nope") == "No rule found with id 'nope'."

    async def test_each_agent_is_stopped_forgotten_and_recorded(self) -> None:
        running = _Actor("door-watcher")
        registry = _Registry(running)
        host = _host(registry)
        host.save_pipeline_rule(
            {
                "rule_id": "r1",
                "task": "when the door opens notify me",
                "agents": ["door-watcher", "gone"],
            }
        )

        reply = await host.delete_pipeline_rule("r1")

        assert running.stopped is True
        assert registry.unregistered == ["id-door-watcher"]
        assert host.removed == ["door-watcher", "gone"]
        assert host.cleared == ["door-watcher"]
        assert host.recorded == [("door-watcher", "pipeline rule 'r1' deleted")]
        assert host.get_pipeline_rules() == {}
        assert reply == (
            "Rule 'r1' deleted. Stopped agents: door-watcher.\nRule was: when the door opens notify me"
        )

    async def test_without_a_registry_nothing_is_stopped(self) -> None:
        host = _host()
        host.save_pipeline_rule({"rule_id": "r1", "task": "t", "agents": ["a"]})

        reply = await host.delete_pipeline_rule("r1")

        assert "Stopped agents: none running." in reply


class TestPlanExpiry:
    def test_a_pending_plan_older_than_a_day_expires_on_read(self) -> None:
        host = _host()
        host.save_pending_plan(
            {"plan_id": "old", "status": "pending", "created_at": time.time() - 2 * 86400}
        )
        host.update_plan_status("missing", "approved")

        assert host.get_pending_plans()["old"]["status"] == "expired"
