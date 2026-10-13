"""An agent's stored state is brought up to the version its code expects.

The version is kept in the state itself, under `_state_version`, so it moves
with the agent and reads the same from main's pickle and a node's JSON. Each
step from the stored version to the declared one runs in order on a copy, and
the result is written only once every step has worked: a failed step leaves the
stored state as it was and stops the agent starting.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from wactorz.agents.dynamic.agent import DynamicAgent
from wactorz.core.actor import Actor, ActorState
from wactorz.core.persistence import PersistenceAPI, WactorzDB
from wactorz.core.persistence.pickle_store import PickleStore, read_state_file
from wactorz.core.state_versions import STATE_VERSION_KEY, StateUpgradeError, upgraded
from wactorz.node.agent import NodeAgent
from wactorz.node.runner import NodeRunner

AGENT = "thermostat"


def _rename_then_scale(state: dict[str, Any], from_version: int) -> dict[str, Any]:
    """v0 → v1 renames `temp` to `celsius`; v1 → v2 adds `fahrenheit`."""
    if from_version == 0:
        state["celsius"] = state.pop("temp")
    elif from_version == 1:
        state["fahrenheit"] = state["celsius"] * 9 / 5 + 32
    return state


def _refuse(_state: dict[str, Any], from_version: int) -> dict[str, Any]:
    if from_version == 1:
        raise KeyError("celsius")
    return {"celsius": 20, "touched": True}


class TestTheSteps:
    async def test_each_step_runs_in_order_and_the_version_is_stamped(self) -> None:
        new = await upgraded(AGENT, {"temp": 20}, 2, _rename_then_scale)

        assert new == {"celsius": 20, "fahrenheit": 68.0, STATE_VERSION_KEY: 2}

    async def test_only_the_steps_still_owed_run(self) -> None:
        new = await upgraded(AGENT, {"celsius": 10, STATE_VERSION_KEY: 1}, 2, _rename_then_scale)

        assert new == {"celsius": 10, "fahrenheit": 50.0, STATE_VERSION_KEY: 2}

    async def test_state_already_current_needs_nothing_written(self) -> None:
        assert await upgraded(AGENT, {STATE_VERSION_KEY: 2}, 2, _rename_then_scale) is None

    async def test_state_newer_than_the_code_is_left_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert await upgraded(AGENT, {STATE_VERSION_KEY: 3}, 2, _rename_then_scale) is None
        assert "version 3 and its code expects 2" in caplog.text

    async def test_a_failed_step_is_named_and_the_state_given_is_unchanged(self) -> None:
        state = {"temp": 20, "readings": [1, 2]}

        with pytest.raises(StateUpgradeError, match="from version 1 to 2") as raised:
            await upgraded(AGENT, state, 2, _refuse)

        assert raised.value.from_version == 1
        assert state == {"temp": 20, "readings": [1, 2]}

    async def test_a_step_must_return_the_state(self) -> None:
        with pytest.raises(StateUpgradeError, match="returned NoneType"):
            await upgraded(AGENT, {"temp": 20}, 1, lambda _s, _v: None)

    async def test_an_upgrade_may_be_a_coroutine(self) -> None:
        async def later(state: dict[str, Any], _from: int) -> dict[str, Any]:
            await asyncio.sleep(0)
            return {**state, "upgraded": True}

        new = await upgraded(AGENT, {"temp": 20}, 1, later)

        assert new == {"temp": 20, "upgraded": True, STATE_VERSION_KEY: 1}

    async def test_no_state_is_only_stamped(self) -> None:
        # A new agent has nothing to upgrade; its upgrade is not asked to cope.
        assert await upgraded(AGENT, {}, 2, _refuse) == {STATE_VERSION_KEY: 2}

    async def test_no_upgrade_function_is_only_stamped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            new = await upgraded(AGENT, {"temp": 20}, 1, None)

        assert new == {"temp": 20, STATE_VERSION_KEY: 1}
        assert "marked as version 1; nothing to upgrade" in caplog.text
        assert "upgraded from" not in caplog.text


class _Thermostat(Actor):
    state_version = 2

    def upgrade_state(self, state: dict[str, Any], from_version: int) -> dict[str, Any]:
        return _rename_then_scale(state, from_version)

    async def handle_message(self, message: Any) -> None:  # pragma: no cover - never sent one
        return None


class _Broken(_Thermostat):
    def upgrade_state(self, state: dict[str, Any], from_version: int) -> dict[str, Any]:
        return _refuse(state, from_version)


async def _started(agent: Actor) -> None:
    """What `Actor.start` does before the agent's own code runs."""
    await agent._load_persistent_state()
    await agent._bring_state_up_to_date(agent.state_version, agent._class_upgrade())


class TestANativeAgent:
    async def test_its_state_is_upgraded_and_written(self, tmp_path: Path) -> None:
        _Thermostat(name=AGENT, persistence_dir=str(tmp_path)).persist("temp", 20)

        agent = _Thermostat(name=AGENT, persistence_dir=str(tmp_path))
        await _started(agent)

        again = _Thermostat(name=AGENT, persistence_dir=str(tmp_path))
        await again._load_persistent_state()
        assert again._persistent_state == {"celsius": 20, "fahrenheit": 68.0, "_state_version": 2}

    async def test_start_runs_the_upgrade_before_on_start(self, tmp_path: Path) -> None:
        seen: list[Any] = []

        class _Watching(_Thermostat):
            async def on_start(self) -> None:
                seen.append(self.recall("celsius"))

        _Watching(name=AGENT, persistence_dir=str(tmp_path)).persist("temp", 21)
        agent = _Watching(name=AGENT, persistence_dir=str(tmp_path))
        try:
            await agent.start()
        finally:
            await agent.stop()

        assert seen == [21]

    async def test_a_failed_upgrade_stops_the_start_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        _Broken(name=AGENT, persistence_dir=str(tmp_path)).persist("temp", 20)
        agent = _Broken(name=AGENT, persistence_dir=str(tmp_path))

        with pytest.raises(StateUpgradeError):
            await _started(agent)

        again = _Broken(name=AGENT, persistence_dir=str(tmp_path))
        await again._load_persistent_state()
        assert again._persistent_state == {"temp": 20}

    async def test_an_agent_that_declares_no_version_is_left_alone(self, tmp_path: Path) -> None:
        class _Plain(Actor):
            async def handle_message(self, message: Any) -> None:  # pragma: no cover
                return None

        _Plain(name=AGENT, persistence_dir=str(tmp_path)).persist("temp", 20)
        agent = _Plain(name=AGENT, persistence_dir=str(tmp_path))

        await _started(agent)

        assert agent._persistent_state == {"temp": 20}

    async def test_with_a_store_only_its_own_keys_are_reshaped(self, tmp_path: Path) -> None:
        with WactorzDB(str(tmp_path / "wactorz.db")) as db:
            api = PersistenceAPI(db, PickleStore(str(tmp_path)), AGENT)
            api.set("temp", 20)
            api.set("conversation_history", [{"role": "user", "content": "hi"}])
            agent = _Thermostat(name=AGENT, persistence_dir=str(tmp_path))
            agent._persistence_api = api

            await _started(agent)

            assert api.get("temp") is None
            assert api.get("celsius") == 20
            assert api.get(STATE_VERSION_KEY) == 2
            assert api.get("conversation_history") == [{"role": "user", "content": "hi"}]

    async def test_with_a_store_the_upgrade_is_on_disk_before_the_agent_runs(
        self, tmp_path: Path
    ) -> None:
        with WactorzDB(str(tmp_path / "wactorz.db")) as db:
            api = PersistenceAPI(db, PickleStore(str(tmp_path)), AGENT)
            api.set("temp", 20)
            agent = _Thermostat(name=AGENT, persistence_dir=str(tmp_path))
            agent._persistence_api = api

            await _started(agent)

            on_disk = read_state_file(tmp_path / AGENT / "state.pkl").values
            assert on_disk == {"celsius": 20, "fahrenheit": 68.0, STATE_VERSION_KEY: 2}


THERMOSTAT_PROGRAM = """
STATE_VERSION = 1

def upgrade_state(state, from_version):
    state["celsius"] = state.pop("temp")
    return state

async def setup(agent):
    agent.state["seen"] = agent.recall("celsius")
"""

BROKEN_PROGRAM = """
STATE_VERSION = 1

def upgrade_state(state, from_version):
    raise ValueError("cannot read the old shape")

async def setup(agent):
    agent.state["setup_ran"] = True
"""


def _dynamic(tmp_path: Path, code: str) -> DynamicAgent:
    agent = DynamicAgent(name=AGENT, code=code, poll_interval=0, persistence_dir=str(tmp_path))
    agent.state = ActorState.RUNNING
    return agent


async def _until(predicate: Any) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not met in time"
        await asyncio.sleep(0)


class TestAGeneratedAgent:
    async def test_setup_sees_the_upgraded_state(self, tmp_path: Path) -> None:
        DynamicAgent(name=AGENT, code="", persistence_dir=str(tmp_path)).persist("temp", 19)
        agent = _dynamic(tmp_path, THERMOSTAT_PROGRAM)
        await agent._load_persistent_state()

        await agent.on_start()
        await _until(lambda: "seen" in agent._api.state)
        await asyncio.gather(*agent._program_tasks, return_exceptions=True)

        assert agent._api.state["seen"] == 19
        assert agent.recall(STATE_VERSION_KEY) == 1

    async def test_a_failed_upgrade_fails_the_agent_before_setup(self, tmp_path: Path) -> None:
        DynamicAgent(name=AGENT, code="", persistence_dir=str(tmp_path)).persist("temp", 19)
        agent = _dynamic(tmp_path, BROKEN_PROGRAM)
        await agent._load_persistent_state()

        await agent.on_start()

        assert agent.state is ActorState.FAILED
        assert "setup_ran" not in agent._api.state
        assert agent.recall("temp") == 19
        assert agent.recall(STATE_VERSION_KEY) is None


class TestANodeAgent:
    async def test_the_json_state_file_is_upgraded(self, tmp_path: Path) -> None:
        runner = NodeRunner("localhost", 1883, "node-a", state_dir=str(tmp_path))
        NodeAgent({"name": AGENT, "code": ""}, runner).persist("temp", 18)
        agent = NodeAgent({"name": AGENT, "code": THERMOSTAT_PROGRAM}, runner)
        agent.state = ActorState.RUNNING

        await agent.on_start()
        await _until(lambda: "seen" in agent._api.state)
        await asyncio.gather(*agent._program_tasks, return_exceptions=True)

        reread = NodeAgent({"name": AGENT, "code": ""}, runner)
        assert reread._persistent_state == {"celsius": 18, STATE_VERSION_KEY: 1}
