# SPDX-License-Identifier: Apache-2.0
"""A library agent writes Wactorz's topics bare too, and files what it records without the namespace.

A rule's trigger, a recorder's topics, a function's ``publishes``: spawn configs
and classes name topics as a person or the model writes them. `Actor`'s public
edges -- publish, subscribe, a window, a manifest -- take them as they travel
(`wire_topic`), as `AgentAPI` does for a program. What a recorder or a trial
files a reading under is named without the namespace (`display_topic`), so it
keeps its name and its history whatever the namespace.

The namespace is set by patching it here: the helpers read it on each call.
"""

from pathlib import Path
from typing import Any

import pytest

from wactorz.agents import shadow_serving
from wactorz.agents.recorder_agent import RecorderAgent
from wactorz.core import topics
from wactorz.core.actor import Actor, Message
from wactorz.core.manifests import AgentManifest
from wactorz.core.topic_bus import StreamWindow


@pytest.fixture(autouse=True)
def namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topics, "NAMESPACE", "lab")


class _Library(Actor):
    """A library agent of the plainest kind, recording what it would send."""

    def __init__(self, persistence_dir: str) -> None:
        super().__init__(name="library", persistence_dir=persistence_dir)
        self.sent: list[str] = []
        self.manifests: list[AgentManifest] = []
        self.bound: list[str] = []

    async def handle_message(self, msg: Message) -> None:
        return None

    async def _mqtt_publish(self, topic: str, payload: Any, **_kwargs: Any) -> None:
        self.sent.append(topic)

    async def _send_manifest(self, manifest: AgentManifest) -> None:
        self.manifests.append(manifest)


class _Hub:
    def __init__(self, bound: list[str]) -> None:
        self._bound = bound

    def bind(self, topic: str, _callback: Any, **_kwargs: Any) -> None:
        self._bound.append(topic)


@pytest.fixture(name="agent")
def agent_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Library:
    agent = _Library(str(tmp_path))
    monkeypatch.setattr(agent, "_make_hub", lambda: _Hub(agent.bound))
    return agent


async def _ignore(_payload: Any) -> None:
    return None


class TestALibraryAgentsEdges:
    async def test_it_publishes_under_the_namespace(self, agent: _Library) -> None:
        await agent.publish("agents/a1/data/level", {"v": 1})
        await agent.publish("sensors/air", {"v": 1})

        assert agent.sent == ["lab/agents/a1/data/level", "sensors/air"]

    def test_it_subscribes_under_the_namespace(self, agent: _Library) -> None:
        agent.subscribe("models/air/drift", _ignore)
        agent.subscribe("home/#", _ignore)

        assert agent.bound == ["lab/models/air/drift", "home/#"]

    def test_a_window_is_one_whichever_way_it_is_written(
        self, agent: _Library, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(StreamWindow, "start", lambda *_args: None)

        first = agent.window("agents/a1/data/level")
        again = agent.window("lab/agents/a1/data/level")

        assert first is again

    async def test_its_manifest_names_its_topics_as_they_travel(self, agent: _Library) -> None:
        await agent.publish_manifest(
            publishes=["agents/a1/data/level"], subscribes=["models/air/drift", "sensors/#"]
        )

        (manifest,) = agent.manifests
        assert manifest.get("publishes") == ["lab/agents/a1/data/level"]
        assert manifest.get("subscribes") == ["lab/models/air/drift", "sensors/#"]


class TestWhatIsFiled:
    @pytest.mark.parametrize(
        ("topic", "source"),
        [
            ("lab/agents/a1/data/level", "agents/a1/data/level"),
            ("agents/a1/data/level", "agents/a1/data/level"),
            ("lab/agents/+/data/#", "agents"),
            ("sensors/imu/#", "sensors/imu"),
            ("lab/temperature", "lab/temperature"),
        ],
    )
    def test_a_recorder_files_a_reading_without_the_namespace(
        self, tmp_path: Path, topic: str, source: str
    ) -> None:
        recorder = RecorderAgent(persistence_dir=str(tmp_path), topics=topic)

        assert recorder.source_of({"value": 1}, topic) == source

    def test_a_named_source_in_the_payload_still_wins(self, tmp_path: Path) -> None:
        recorder = RecorderAgent(persistence_dir=str(tmp_path))

        assert recorder.source_of({"source": "left-wrist"}, "lab/agents/a1/data") == "left-wrist"

    @pytest.mark.parametrize(
        ("topic", "source"),
        [("lab/agents/a1/data/level", "agents/a1/data/level"), ("sensors/imu", "sensors/imu")],
    )
    def test_a_trial_files_its_pairs_without_the_namespace(self, topic: str, source: str) -> None:
        assert shadow_serving.source_of({"x": 1}, topic) == source
