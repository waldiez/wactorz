"""An agent that ends by itself leaves the dashboard, the same as a deleted one.

Deleting through the UI removes the monitor's entry for the agent. An agent that
ends on its own schedule — a planner reaching its lifetime, a one-off actuator
finishing its request, a schedule that fires once — only tears itself down, and
the monitor's entry outlived it. The card then flickered: the REST actor list is
built from the registry and no longer named the agent, so each reconcile dropped
the card, while every state patch still carried the stale entry and put it back.

The signal between the two is the retained manifest. It is the agent's own
declaration that it exists, and withdrawing it is the one removal message every
ending already publishes — including a node's runner, which runs in another
process on another machine where none of `web/` can be called. So the monitor
listens for the withdrawal rather than each ending reaching in here.

Retiring is deliberately not one of those endings: an agent past its restart
budget is what the operator is being asked to look at, and a card that vanishes
takes the notification's subject with it.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.one_off_actuator_agent import OneOffActuatorAgent
from wactorz.agents.planner.agent import PlannerAgent
from wactorz.agents.scheduled_agent import ScheduledAgent
from wactorz.core.actor import Actor, Message
from wactorz.web import events, mqtt, runtime


class RecordingMQTT:
    """Stands in for the actor's broker client, keeping what it was asked to send."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any, bool, int]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, payload, retain, qos))

    async def disconnect(self) -> None:
        return None

    def withdrawals(self) -> list[str]:
        """The topics this client was asked to take a retained message back on."""
        return [
            topic for topic, payload, retain, _qos in self.published if retain and payload == b""
        ]


class Ending(Actor):
    """A bare actor, for exercising the withdrawal on its own."""

    async def handle_message(self, msg: Message) -> None:
        return None


@pytest.fixture(name="isolate_state", autouse=True)
def isolate_state_fixture() -> Any:
    """Give each test the live agent map and tombstone list to itself."""
    agents = dict(runtime.state["agents"])
    feed = list(runtime.state["log_feed"])
    tombstones = list(runtime.deleted_agent_ids)
    runtime.state["agents"] = {}
    runtime.state["log_feed"] = []
    runtime.deleted_agent_ids.clear()
    yield
    runtime.state["agents"] = agents
    runtime.state["log_feed"] = feed
    runtime.deleted_agent_ids.clear()
    runtime.deleted_agent_ids.extend(tombstones)


def seen(agent_id: str, name: str = "worker") -> None:
    """Put an agent on the dashboard the way a heartbeat would."""
    events.record_heartbeat(
        agent_id, {"name": name, "state": "running", "cpu": 1.0, "memory_mb": 8.0}
    )
    assert agent_id in runtime.state["agents"], "the agent has to be on screen to be removed"


class TestTheActorSideOfIt:
    """What an actor publishes when it decides it is finished."""

    async def test_withdrawing_takes_the_retained_manifest_back(self, tmp_path: Path) -> None:
        actor = Ending(name="ending", persistence_dir=str(tmp_path))
        broker = RecordingMQTT()
        actor._mqtt_client = broker  # pyright: ignore[reportAttributeAccessIssue]

        await actor.withdraw_manifest()

        assert broker.withdrawals() == [f"agents/{actor.actor_id}/manifest"]

    async def test_it_is_sent_at_least_once(self, tmp_path: Path) -> None:
        """A lost withdrawal leaves the broker replaying the manifest for ever."""
        actor = Ending(name="ending", persistence_dir=str(tmp_path))
        broker = RecordingMQTT()
        actor._mqtt_client = broker  # pyright: ignore[reportAttributeAccessIssue]

        await actor.withdraw_manifest()

        _topic, _payload, _retain, qos = broker.published[-1]
        assert qos == 1

    async def test_the_topic_is_the_one_every_publisher_uses(self, tmp_path: Path) -> None:
        """An actor publishes its own manifest; a generated agent's API publishes
        one for it. Both write the same topic, so one withdrawal covers either."""
        actor = Ending(name="ending", persistence_dir=str(tmp_path))
        broker = RecordingMQTT()
        actor._mqtt_client = broker  # pyright: ignore[reportAttributeAccessIssue]

        await actor.publish_manifest(description="a worker")
        await actor.withdraw_manifest()

        published, withdrawn = broker.published[0][0], broker.published[1][0]
        assert published == withdrawn


class TestTheEndingsThatRemoveAnAgent:
    """Each ending that takes an agent away withdraws its manifest."""

    async def test_a_planner_that_terminates_itself(self, tmp_path: Path) -> None:
        planner = PlannerAgent(
            llm_provider=None, persistence_dir=str(tmp_path), auto_terminate=False
        )
        broker = RecordingMQTT()
        planner._mqtt_client = broker  # pyright: ignore[reportAttributeAccessIssue]

        await planner._terminate()

        assert f"agents/{planner.actor_id}/manifest" in broker.withdrawals()

    async def test_a_one_off_actuator_that_has_finished(self, tmp_path: Path) -> None:
        agent = OneOffActuatorAgent(
            request="turn on the hall light",
            llm_provider=None,
            task_id="actuate_test",
            reply_to_id="main-actor",
            persistence_dir=str(tmp_path),
        )
        broker = RecordingMQTT()
        agent._mqtt_client = broker  # pyright: ignore[reportAttributeAccessIssue]

        await agent._deferred_stop()

        assert f"agents/{agent.actor_id}/manifest" in broker.withdrawals()

    async def test_a_schedule_that_has_fired_its_last(self, tmp_path: Path) -> None:
        at = (datetime.now().astimezone() + timedelta(days=1)).isoformat()
        agent = ScheduledAgent(
            name="once-only",
            schedule={"type": "once", "at": at},
            persistence_dir=str(tmp_path),
        )
        broker = RecordingMQTT()
        agent._mqtt_client = broker  # pyright: ignore[reportAttributeAccessIssue]

        await agent._self_delete()

        assert f"agents/{agent.actor_id}/manifest" in broker.withdrawals()


class TestTheMonitorReadsTheWithdrawal:
    """What the monitor does with a manifest it is asked to forget."""

    def test_the_entry_goes(self) -> None:
        seen("a1")

        events.parse_topic("agents/a1/manifest", "")

        assert "a1" not in runtime.state["agents"]

    def test_the_snapshot_stops_naming_it(self) -> None:
        """The patch every browser receives is built from this."""
        seen("a1")

        events.parse_topic("agents/a1/manifest", "")

        named = [ag["agent_id"] for ag in events.snapshot(include_totals=False)["agents"]]
        assert "a1" not in named

    def test_a_trailing_frame_does_not_bring_it_back(self) -> None:
        """The flicker itself: a heartbeat already in flight when the agent ended
        recreated the entry, and the next patch drew the card again."""
        seen("a1")
        events.parse_topic("agents/a1/manifest", "")

        events.parse_topic(
            "agents/a1/heartbeat", json.dumps({"name": "worker", "state": "running"})
        )

        assert "a1" not in runtime.state["agents"]

    def test_the_frame_is_the_one_the_dashboard_matches(self) -> None:
        seen("a1")

        event = events.parse_topic("agents/a1/manifest", "")

        assert event == {"type": events.DELETE_AGENT_FRAME, "agent_id": "a1"}

    def test_an_agent_it_never_saw_is_still_tombstoned(self) -> None:
        """The withdrawal can arrive before the agent's first heartbeat is read."""
        events.parse_topic("agents/unknown/manifest", "")

        assert runtime.is_deleted("unknown")

    def test_a_manifest_with_content_is_not_a_withdrawal(self) -> None:
        seen("a1")

        event = events.parse_topic(
            "agents/a1/manifest", json.dumps({"name": "worker", "actor_id": "a1"})
        )

        assert "a1" in runtime.state["agents"]
        assert event is not None and event["type"] == "agent"

    def test_a_respawn_under_the_same_id_is_re_admitted(self) -> None:
        """Spawning the same name again derives the same id, so the tombstone has
        to give way to the new instance's first status."""
        seen("a1")
        events.parse_topic("agents/a1/manifest", "")

        events.parse_topic(
            "agents/a1/status", json.dumps({"name": "worker", "state": "running", "uptime": 0.2})
        )

        assert not runtime.is_deleted("a1")
        assert "a1" in runtime.state["agents"]


class TestTheBrowserIsTold:
    """A patch only adds and updates, so removal needs its own frame."""

    async def test_the_withdrawal_broadcasts_the_delete_frame(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen("a1")
        sent: list[dict[str, Any]] = []

        async def capture(frame: dict[str, Any]) -> None:
            sent.append(frame)

        monkeypatch.setattr(mqtt.ws, "broadcast", capture)

        await mqtt.handle_message("agents/a1/manifest", "")

        assert [frame["type"] for frame in sent] == [events.DELETE_AGENT_FRAME]
        assert sent[0]["agent_id"] == "a1"

    async def test_the_frame_carries_a_snapshot_without_the_agent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen("a1")
        seen("a2", name="other")
        sent: list[dict[str, Any]] = []

        async def capture(frame: dict[str, Any]) -> None:
            sent.append(frame)

        monkeypatch.setattr(mqtt.ws, "broadcast", capture)

        await mqtt.handle_message("agents/a1/manifest", "")

        named = [ag["agent_id"] for ag in sent[0]["state"]["agents"]]
        assert named == ["a2"]
