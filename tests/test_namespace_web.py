# SPDX-License-Identifier: Apache-2.0
"""The dashboard knows no namespace: the server takes it off on the way out and puts it on on the way in.

Only Wactorz's own topics change. A world topic whose first level is the
namespace is somebody else's and passes as it is, both ways.

The namespace is set by patching it here: the helpers read it on each call.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from wactorz.core import topics
from wactorz.web import mqtt as web_mqtt
from wactorz.web import relay
from wactorz.web.wiring import displayed


@pytest.fixture(autouse=True)
def namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(topics, "NAMESPACE", "lab")


class TestTheFramesThePageIsSent:
    @pytest.mark.parametrize(
        ("topic", "shown"),
        [
            ("lab/agents/a1/status", "agents/a1/status"),
            ("lab/system/health", "system/health"),
            ("lab/temperature", "lab/temperature"),
            ("sensors/air", "sensors/air"),
        ],
    )
    async def test_a_frame_shows_own_topics_bare(self, topic: str, shown: str) -> None:
        broadcast = AsyncMock()
        with patch("wactorz.web.ws.broadcast", new=broadcast):
            await web_mqtt.broadcast_mqtt_msg(topic, "{}")

        frame: dict[str, Any] = broadcast.call_args.args[0]
        assert frame["topic"] == shown


class TestWhatAPanelFollows:
    def test_its_filters_are_followed_under_the_namespace(self) -> None:
        try:
            relay.follow(["agents/+/data/#", "mlops/+/state"])

            assert relay.followed() == ("lab/agents/+/data/#", "mlops/+/state")
            assert relay.is_relayed("lab/agents/a1/data/level")
        finally:
            relay.follow([])


class TestTheWiringThePageIsSent:
    def test_every_topic_in_it_is_shown_bare(self) -> None:
        graph: dict[str, Any] = {
            "agents": [
                {
                    "id": "a1",
                    "publishes": ["lab/agents/a1/data/level"],
                    "subscribes": ["sensors/#", "lab/shadow/x"],
                }
            ],
            "topics": [{"topic": "lab/agents/a1/data/level"}, {"topic": "lab/temperature"}],
            "sources": [{"root": "sensors", "topics": ["sensors/#"]}],
            "warnings": [{"kind": "unread", "topic": "lab/agents/a1/data/level"}],
        }

        shown = displayed(graph)

        assert shown["agents"][0]["publishes"] == ["agents/a1/data/level"]
        assert shown["agents"][0]["subscribes"] == ["sensors/#", "shadow/x"]
        assert [t["topic"] for t in shown["topics"]] == ["agents/a1/data/level", "lab/temperature"]
        assert shown["warnings"][0]["topic"] == "agents/a1/data/level"
        assert shown["sources"][0]["topics"] == ["sensors/#"]
