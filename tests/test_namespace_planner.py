# SPDX-License-Identifier: Apache-2.0
"""What the model is shown names Wactorz's topics bare, whatever the namespace.

Contracts hold topics as they travel, with the namespace in front. A topic the
model copies from its prompt into a program with the namespace in it would keep
that namespace after it changed, and the program would publish where nobody
listens. So every topic put in front of the model, or of a person in chat, is
shown bare (`display_topic`); a topic a request names, bare, is listened to as
it travels (`wire_topic`).

The namespace is set by patching it here: the helpers read it on each call.
"""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from wactorz.agents.main.commands.info import show_bus
from wactorz.agents.planner import context as context_mod
from wactorz.agents.planner.agent import _fmt_worker
from wactorz.agents.planner.context import ContextMixin, describe_topic_source
from wactorz.core import topic_bus, topics
from wactorz.core.topic_bus import TopicBus, TopicContract

OWN = "lab/agents/a1/data/level"


@pytest.fixture(autouse=True)
def namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topics, "NAMESPACE", "lab")


class TestWhatTheModelIsShown:
    def test_a_workers_topics_are_bare(self) -> None:
        line = _fmt_worker(
            {
                "name": "w",
                "type": "DynamicAgent",
                "description": "d",
                "publishes": [OWN, "sensors/air"],
                "observed_samples": {OWN: {"fields": {"level": "int"}, "example": {"level": 3}}},
            }
        )

        assert "lab/" not in line
        assert "agents/a1/data/level" in line and "sensors/air" in line

    @pytest.mark.parametrize("found", [1, 2])
    def test_a_resolved_data_source_is_bare(self, found: int) -> None:
        candidates = [{"topic": OWN, "agent": "w", "node": None}] * found

        resolved = describe_topic_source("watch the level", candidates)

        assert resolved is not None
        task, note = resolved
        assert "lab/" not in task and "lab/" not in note
        assert "agents/a1/data/level" in task

    def test_a_sample_is_shown_bare(self) -> None:
        line = context_mod._sample_line(OWN, "published by w", {"level": 3})

        assert "Topic: agents/a1/data/level" in line

    async def test_a_topic_the_request_names_is_listened_to_as_it_travels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        listened: list[str] = []

        async def _collect(
            _host: str, _port: int, wanted: list[tuple[str, str]], _log: str, **_kw: Any
        ) -> dict[str, dict[str, Any]]:
            listened.extend(topic for topic, _ in wanted)
            return {OWN: {"level": 3}}

        monkeypatch.setattr(context_mod, "collect_one_payload_per_topic", _collect)
        planner = cast("ContextMixin", SimpleNamespace(name="planner"))

        lines = await ContextMixin._sample_named_topics(planner, "watch agents/a1/data/level")

        assert listened == [OWN]
        assert "lab/" not in lines[0]


class TestWhatAPersonIsShown:
    async def test_the_bus_command_shows_topics_bare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        bus = TopicBus()
        monkeypatch.setattr(topic_bus, "_topic_bus", bus)
        bus.register_contract(TopicContract(name="w", publishes=[OWN], actor_id="w"))
        bus.register_contract(TopicContract(name="r", subscribes=[OWN], actor_id="r"))

        shown = await show_bus(cast("Any", None), "")

        assert "lab/" not in shown
        assert "agents/a1/data/level" in shown
