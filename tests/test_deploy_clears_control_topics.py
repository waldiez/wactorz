"""A deploy clears the node's control topics before that node subscribes to them.

A node acts on what it finds on `nodes/<name>/spawn` and its siblings as soon as it
connects, and a spawn carries code. Main publishes those without retaining them, so
a retained message on one was put there by something else -- and the access list
generated for a broker of ours names the nodes that exist, which cannot cover a name
no node has taken yet. Clearing them at deploy is what closes that: the node starts
against topics main has just emptied.

`desired_state` is deliberately not cleared: main retains its own there, and
republishes it as soon as the node's heartbeat reports that it checks signatures.
"""

from typing import Any

import pytest

from wactorz.agents.installer_agent import UNRETAINED_CONTROL_TOPICS, InstallerAgent


class _Recorder:
    """An installer whose publishes are recorded rather than sent."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any, bool, int]] = []

    async def publish(self, topic: str, payload: Any, retain: bool = False, qos: int = 0) -> None:
        self.published.append((topic, payload, retain, qos))


@pytest.fixture(name="agent")
def agent_fixture() -> Any:
    agent = InstallerAgent.__new__(InstallerAgent)
    agent.name = "installer"
    recorder = _Recorder()
    agent._mqtt_publish = recorder.publish  # pyright: ignore[reportAttributeAccessIssue]  # records what a deploy would send
    agent.recorder = recorder  # pyright: ignore[reportAttributeAccessIssue]  # for the assertions below
    return agent


class TestClearingThem:
    async def test_every_topic_main_does_not_retain_is_cleared(self, agent: Any) -> None:
        await agent._clear_planted_control("rpi")

        cleared = {topic for topic, *_ in agent.recorder.published}
        assert cleared == {f"nodes/rpi/{leaf}" for leaf in UNRETAINED_CONTROL_TOPICS}

    async def test_they_are_cleared_the_way_mqtt_clears_a_retained_message(
        self, agent: Any
    ) -> None:
        # An empty payload, published retained: anything else leaves the message there.
        await agent._clear_planted_control("rpi")

        for _, payload, retain, qos in agent.recorder.published:
            assert payload == b""
            assert retain is True
            assert qos == 1

    async def test_the_desired_state_is_left_alone(self, agent: Any) -> None:
        # Main retains its own there, and republishes it signed once the node reports
        # that it checks; clearing it would drop a node's agents on every redeploy.
        await agent._clear_planted_control("rpi")

        assert "desired_state" not in UNRETAINED_CONTROL_TOPICS
        assert not any("desired_state" in topic for topic, *_ in agent.recorder.published)

    async def test_only_this_node_is_touched(self, agent: Any) -> None:
        await agent._clear_planted_control("rpi")

        assert all(topic.startswith("nodes/rpi/") for topic, *_ in agent.recorder.published)


def test_the_deploy_clears_them_before_it_starts_the_runner() -> None:
    # After that, the node is running and subscribed: a message cleared then has
    # already been delivered.
    import inspect

    source = inspect.getsource(InstallerAgent._node_deploy)
    assert source.index("_clear_planted_control") < source.index("node_service.install")
