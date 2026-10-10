"""A scheduled agent at run time: validating, firing, restarting, and ending.

`tests/test_schedule_times.py` pins the clock arithmetic. This pins what the
agent does with it. A malformed schedule is refused at spawn rather than at the
first fire, which may be days later. A fire publishes one event and records it,
so a restart resumes from the last fire rather than from zero.

A one-shot schedule is the case with an ending. Missed by a little — the
process was restarting at the moment — it still fires; missed by more, or
already fired in an earlier run, it removes itself instead of lingering as a
card for a moment that has passed.
"""

import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents import scheduled_agent
from wactorz.agents.scheduled_agent import ScheduledAgent, _next_fire_cron
from wactorz.core.actor import ActorState, Message, MessageType

DAILY = {"type": "daily", "at": "17:00", "tz": "UTC"}

#: Cron schedules are an optional extra; without croniter they are refused by design.
needs_croniter = pytest.mark.skipif(
    importlib.util.find_spec("croniter") is None, reason="the cron extra is not installed"
)


class _Broker:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, json.loads(payload) if payload else payload))

    def on(self, topic: str) -> list[Any]:
        return [p for t, p in self.published if t == topic]


def _agent(tmp_path: Path, schedule: dict[str, Any], **kwargs: Any) -> ScheduledAgent:
    kwargs.setdefault("name", "evening")
    agent = ScheduledAgent(schedule=schedule, persistence_dir=str(tmp_path), **kwargs)
    agent._mqtt_client = _Broker()
    return agent


def _broker(agent: ScheduledAgent) -> _Broker:
    broker = agent._mqtt_client
    assert isinstance(broker, _Broker)
    return broker


def _utc_iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).replace(tzinfo=None).isoformat()


def _record_self_delete(agent: ScheduledAgent) -> list[bool]:
    deleted: list[bool] = []

    async def _delete() -> None:
        deleted.append(True)

    agent._self_delete = _delete  # pyright: ignore[reportAttributeAccessIssue]
    return deleted


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _stop_loop(agent: ScheduledAgent) -> None:
    task = agent._loop_task
    if task is not None and not task.done():
        task.cancel()
        # `asyncio.wait`, not `wait_for`, which can lose a cancellation on 3.10/3.11.
        await asyncio.wait({task}, timeout=5)


class TestValidationAtSpawn:
    def test_a_missing_schedule_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="requires a 'schedule' dict"):
            ScheduledAgent(schedule=None, persistence_dir=str(tmp_path))

    @pytest.mark.parametrize(
        ("schedule", "message"),
        [
            ({"type": "daily"}, "requires 'at'"),
            ({"type": "weekly", "days": ["mon"]}, "requires 'at'"),
            ({"type": "interval"}, "requires 'seconds'"),
            ({"type": "interval", "every": "soon"}, "requires 'seconds'"),
            ({"type": "once"}, "requires 'at'"),
            ({"type": "cron"}, "requires 'expr'"),
            pytest.param(
                {"type": "cron", "expr": "not a cron"},
                "Invalid cron expression",
                marks=needs_croniter,
            ),
            ({"type": "hourly"}, "Unknown schedule type"),
            ({}, "Unknown schedule type"),
        ],
    )
    def test_a_malformed_schedule_is_refused_by_name(
        self, tmp_path: Path, schedule: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message) as refused:
            ScheduledAgent(schedule=schedule, name="broken", persistence_dir=str(tmp_path))

        assert "Invalid schedule for broken" in str(refused.value)

    @pytest.mark.parametrize(
        "schedule",
        [
            {"type": "weekly", "at": "08:30", "days": ["mon", "friday"]},
            {"type": "interval", "every_seconds": 60},
            {"type": "interval", "every": "5m"},
            pytest.param({"type": "cron", "expression": "0 17 * * *"}, marks=needs_croniter),
            {"type": "once", "at": "2099-01-01T00:00:00"},
        ],
    )
    def test_every_documented_form_is_accepted(
        self, tmp_path: Path, schedule: dict[str, Any]
    ) -> None:
        agent = _agent(tmp_path, schedule)

        assert agent.description == f"Scheduled trigger ({schedule['type']})"
        assert agent._publish_topic == "schedule/evening/fired"

    def test_a_topic_and_description_can_be_given(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, DAILY, publish_topic="lights/on", description="lights at five")

        assert agent._publish_topic == "lights/on"
        assert agent.description == "lights at five"

    def test_cron_without_croniter_explains_the_structured_alternative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "croniter", None)

        with pytest.raises(ValueError, match="structured schedule"):
            _next_fire_cron(datetime.now(timezone.utc), "0 17 * * *")


class TestFiring:
    async def test_a_fire_publishes_the_event_and_records_it(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, DAILY)
        await agent.on_start()
        await _stop_loop(agent)
        fired_at = datetime(2026, 9, 17, 17, 0, tzinfo=timezone.utc)

        await agent._fire(now_utc=fired_at, manual=True)

        (event,) = _broker(agent).on("schedule/evening/fired")
        assert event == {
            "fired_at": fired_at.isoformat(),
            "schedule_type": "daily",
            "agent": "evening",
            "manual": True,
        }
        assert agent.recall("_schedule_state") == {
            "last_fire_iso": fired_at.isoformat(),
            "fire_count": 1,
        }

    async def test_a_failed_save_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path, DAILY)
        await agent.on_start()
        await _stop_loop(agent)

        def _refuse(*_args: Any) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(agent, "persist", _refuse)

        await agent._fire(now_utc=datetime.now(timezone.utc))

        assert agent._state.fire_count == 1

    async def test_a_restart_resumes_the_count(self, tmp_path: Path) -> None:
        first = _agent(tmp_path, DAILY)
        await first.on_start()
        await _stop_loop(first)
        await first._fire(now_utc=datetime.now(timezone.utc))

        second = _agent(tmp_path, DAILY)
        await second._load_persistent_state()
        await second.on_start()
        await _stop_loop(second)

        assert second._state.fire_count == 1
        assert second._last_fire_local(timezone.utc) is not None

    async def test_unreadable_saved_state_starts_fresh(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, DAILY)
        agent.persist("_schedule_state", ["not", "a", "dict"])

        await agent.on_start()
        await _stop_loop(agent)

        assert agent._state.fire_count == 0

    async def test_a_naive_last_fire_is_read_as_utc(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, DAILY)
        await agent.on_start()
        await _stop_loop(agent)
        agent._state.last_fire_iso = "2026-09-17T17:00:00"

        last = agent._last_fire_local(timezone.utc)

        assert last == datetime(2026, 9, 17, 17, 0, tzinfo=timezone.utc)

    async def test_an_unreadable_last_fire_is_ignored(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, DAILY)
        await agent.on_start()
        await _stop_loop(agent)
        agent._state.last_fire_iso = "not a timestamp"

        assert agent._last_fire_local(timezone.utc) is None


class TestOneShotStart:
    async def test_already_fired_in_an_earlier_run_it_removes_itself(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, {"type": "once", "at": "2099-01-01T00:00:00", "tz": "UTC"})
        agent.persist("_schedule_state", {"fire_count": 1})
        deleted = _record_self_delete(agent)

        await agent.on_start()
        await _settle()

        assert deleted == [True]
        assert agent._loop_task is None

    async def test_missed_by_a_little_it_fires_then_removes_itself(self, tmp_path: Path) -> None:
        agent = _agent(
            tmp_path, {"type": "once", "at": _utc_iso(-timedelta(seconds=60)), "tz": "UTC"}
        )
        deleted = _record_self_delete(agent)

        await agent.on_start()
        await _settle()

        assert len(_broker(agent).on("schedule/evening/fired")) == 1
        assert deleted == [True]

    async def test_missed_by_a_lot_it_removes_itself_without_firing(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, {"type": "once", "at": _utc_iso(-timedelta(hours=2)), "tz": "UTC"})
        deleted = _record_self_delete(agent)

        await agent.on_start()
        await _settle()

        assert _broker(agent).on("schedule/evening/fired") == []
        assert deleted == [True]

    async def test_a_future_one_shot_starts_its_loop(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, {"type": "once", "at": _utc_iso(timedelta(days=1)), "tz": "UTC"})

        await agent.on_start()

        assert agent._loop_task is not None
        await _stop_loop(agent)

    async def test_a_broken_one_shot_check_still_starts_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path, {"type": "once", "at": _utc_iso(timedelta(days=1)), "tz": "UTC"})

        def _broken(_at: str, _tz: Any) -> datetime:
            raise ValueError("clock unavailable")

        monkeypatch.setattr(scheduled_agent, "_next_fire_once", _broken)

        await agent.on_start()

        assert agent._loop_task is not None
        await _stop_loop(agent)


class TestRunLoop:
    async def test_a_manual_trigger_fires_without_waiting_for_the_schedule(
        self, tmp_path: Path
    ) -> None:
        agent = _agent(tmp_path, DAILY)
        agent.state = ActorState.RUNNING
        await agent.on_start()
        await _settle()

        agent._manual_trigger_event.set()
        for _ in range(50):
            if _broker(agent).on("schedule/evening/fired"):
                break
            await asyncio.sleep(0)

        (event,) = _broker(agent).on("schedule/evening/fired")
        assert event["manual"] is True
        await agent.on_stop()
        assert agent._loop_task is not None and agent._loop_task.done()

    async def test_a_due_one_shot_fires_once_and_removes_itself(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, {"type": "interval", "seconds": 1})
        agent.state = ActorState.RUNNING
        await agent.on_start()
        await _stop_loop(agent)
        agent._schedule = {"type": "once", "at": _utc_iso(-timedelta(seconds=1)), "tz": "UTC"}
        agent._tz = timezone.utc
        deleted = _record_self_delete(agent)

        loop = asyncio.create_task(agent._run_loop())
        done, _ = await asyncio.wait({loop}, timeout=5)
        await _settle()

        assert loop in done
        assert len(_broker(agent).on("schedule/evening/fired")) == 1
        assert deleted == [True]

    async def test_waking_before_the_deadline_goes_back_to_sleep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path, {"type": "interval", "seconds": 3600})
        agent.state = ActorState.RUNNING
        await agent.on_start()
        await _stop_loop(agent)
        monkeypatch.setattr(scheduled_agent, "_MAX_SLEEP_S", 0.001)
        rounds: list[int] = []
        real_compute = agent._compute_next_fire

        def _compute(now: datetime, last: datetime | None) -> datetime:
            rounds.append(1)
            if len(rounds) == 3:
                agent.state = ActorState.STOPPED
            return real_compute(now, last)

        monkeypatch.setattr(agent, "_compute_next_fire", _compute)

        loop = asyncio.create_task(agent._run_loop())
        done, _ = await asyncio.wait({loop}, timeout=5)

        assert loop in done
        assert len(rounds) == 3
        assert _broker(agent).on("schedule/evening/fired") == []

    async def test_an_error_backs_off_instead_of_ending_the_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path, DAILY)
        agent.state = ActorState.RUNNING
        await agent.on_start()
        await _stop_loop(agent)
        backoffs: list[float] = []
        real_sleep = asyncio.sleep

        async def _sleep(delay: float) -> None:
            backoffs.append(delay)
            agent.state = ActorState.STOPPED
            await real_sleep(0)

        def _broken(now: datetime, last: datetime | None) -> datetime:
            raise RuntimeError("clock went backwards")

        monkeypatch.setattr(scheduled_agent.asyncio, "sleep", _sleep)
        monkeypatch.setattr(agent, "_compute_next_fire", _broken)

        loop = asyncio.create_task(agent._run_loop())
        done, _ = await asyncio.wait({loop}, timeout=5)

        assert loop in done
        assert backoffs == [30]

    async def test_stopping_reports_a_loop_that_crashed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        agent = _agent(tmp_path, DAILY)

        async def _fails_while_unwinding() -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed") from None

        agent._loop_task = asyncio.create_task(_fails_while_unwinding())
        await asyncio.sleep(0)

        await agent.on_stop()

        assert agent._loop_task.done()
        assert "scheduling loop ended in error: cleanup failed" in caplog.text


class TestSelfDelete:
    async def test_it_leaves_the_registry_and_mains_spawn_list_then_withdraws(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path, DAILY)
        removed: list[str] = []
        stopped: list[bool] = []

        class _Registry:
            async def unregister(self, actor_id: str) -> None:
                removed.append(actor_id)

        class _Main:
            def _remove_from_spawn_registry(self, name: str) -> None:
                removed.append(name)

        async def _stop() -> None:
            stopped.append(True)

        real_sleep = asyncio.sleep

        async def _instant(_delay: float) -> None:
            await real_sleep(0)

        agent._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setattr(scheduled_agent, "find_main_actor", lambda _r: _Main())
        monkeypatch.setattr(scheduled_agent.asyncio, "sleep", _instant)
        monkeypatch.setattr(agent, "stop", _stop)

        await agent._self_delete()

        assert removed == [agent.actor_id, "evening"]
        assert stopped == [True]
        assert _broker(agent).published[-1] == (f"agents/{agent.actor_id}/manifest", b"")

    async def test_teardown_failures_do_not_stop_the_removal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _agent(tmp_path, DAILY)
        stopped: list[bool] = []

        class _Registry:
            async def unregister(self, actor_id: str) -> None:
                raise RuntimeError("locked")

        class _Main:
            def _remove_from_spawn_registry(self, name: str) -> None:
                raise RuntimeError("db gone")

        async def _stop() -> None:
            stopped.append(True)

        real_sleep = asyncio.sleep

        async def _instant(_delay: float) -> None:
            await real_sleep(0)

        agent._registry = _Registry()  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setattr(scheduled_agent, "find_main_actor", lambda _r: _Main())
        monkeypatch.setattr(scheduled_agent.asyncio, "sleep", _instant)
        monkeypatch.setattr(agent, "stop", _stop)

        await agent._self_delete()

        assert stopped == [True]


class TestMessages:
    @staticmethod
    def _replies(agent: ScheduledAgent) -> list[tuple[str, dict[str, Any]]]:
        sent: list[tuple[str, dict[str, Any]]] = []

        async def _send(target: str, msg_type: MessageType, payload: Any = None) -> bool:
            assert msg_type == MessageType.RESULT
            sent.append((target, payload))
            return True

        agent.send = _send  # pyright: ignore[reportAttributeAccessIssue]
        return sent

    @staticmethod
    async def _started(tmp_path: Path, schedule: dict[str, Any]) -> ScheduledAgent:
        agent = _agent(tmp_path, schedule)
        await agent.on_start()
        await _stop_loop(agent)
        return agent

    async def test_trigger_queues_a_manual_fire(self, tmp_path: Path) -> None:
        agent = await self._started(tmp_path, DAILY)
        sent = self._replies(agent)

        await agent.handle_message(
            Message(
                type=MessageType.TASK,
                sender_id="main",
                payload={"action": "Fire Now", "_task_id": "t1"},
            )
        )

        assert agent._manual_trigger_event.is_set()
        assert sent == [("main", {"result": "Manual trigger queued for evening", "_task_id": "t1"})]

    async def test_info_describes_the_next_fire(self, tmp_path: Path) -> None:
        agent = await self._started(tmp_path, DAILY)
        sent = self._replies(agent)

        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload={"text": "next"})
        )

        ((_, reply),) = sent
        info = reply["info"]
        assert info["publish_topic"] == "schedule/evening/fired"
        assert info["next_fire"].endswith("17:00:00+00:00")
        assert info["fire_count"] == 0

    async def test_info_that_cannot_be_computed_is_an_error_reply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = await self._started(tmp_path, DAILY)
        sent = self._replies(agent)

        def _broken(now: datetime, last: datetime | None) -> datetime:
            raise ValueError("bad clock")

        monkeypatch.setattr(agent, "_compute_next_fire", _broken)

        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="main", payload={"action": "info"})
        )

        assert sent == [("main", {"error": "bad clock"})]

    async def test_an_unknown_action_is_an_error_reply(self, tmp_path: Path) -> None:
        agent = await self._started(tmp_path, DAILY)
        sent = self._replies(agent)

        await agent.handle_message(Message(type=MessageType.TASK, sender_id="main", payload="x"))

        assert sent == [("main", {"error": "Unknown action: ''"})]

    async def test_nobody_to_reply_to_and_non_tasks_get_nothing(self, tmp_path: Path) -> None:
        agent = await self._started(tmp_path, DAILY)
        sent = self._replies(agent)

        await agent.handle_message(
            Message(type=MessageType.TASK, sender_id="", payload={"action": "trigger"})
        )
        await agent.handle_message(Message(type=MessageType.HEARTBEAT, sender_id="main"))

        assert sent == []
