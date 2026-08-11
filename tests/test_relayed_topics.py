"""Only topics a browser acts on are forwarded to it.

The server subscribes to four whole subtrees; the dashboard reads a fixed set
within them. The gap is not theoretical — it is whatever every agent in the
system happens to publish, so it widens with each new agent rather than with any
route table.

The allowed set below is written out one topic at a time on purpose. It mirrors
two things in the frontend — `ServerEventRouter`, and the feed-only mappers the
`raw` fallback dispatches in `agents/mapping.ts` — and a mismatch with either
fails silently: the frame is dropped before it arrives, so the code that would
have rendered it never runs. The last class here checks both mirrors hold.
"""

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import pytest

from wactorz.web import mqtt, relay, runtime

CONSUMED = [
    "agents/a1/heartbeat",
    "agents/a1/status",
    "agents/a1/alert",
    "agents/a1/chat",
    "agents/a1/spawn",
    "agents/a1/metrics",
    "agents/a1/logs",
    "agents/a1/completed",
    # Feed-only suffixes consumed by the raw-fallback mappers in
    # frontend/src/agents/mapping.ts, not by ServerEventRouter.
    "agents/a1/actuations",
    "agents/a1/anomaly",
    "system/qa-flag",
    "system/spawn",
    "system/health",
    "system/host",
    "nodes/pi-kitchen/heartbeat",
    "homeassistant/state_changes",
    "homeassistant/state_changes/light.kitchen",
]

IGNORED = [
    "agents/a1/internal-state",
    "agents/a1/llm_request",
    "agents/a1/commands",
    "system/secrets",
    "system/anything-else",
    "nodes/pi-kitchen/desired_state",
    "main/llm_request",
    "io/chat",
    "custom/detections/front-door",
    "agents/a1",
    "agents/a1/metrics/extra",
]


class TestWhatABrowserIsSent:
    @pytest.mark.parametrize("topic", CONSUMED)
    def test_a_topic_the_dashboard_reads(self, topic: str) -> None:
        assert relay.is_relayed(topic)

    @pytest.mark.parametrize("topic", IGNORED)
    def test_a_topic_it_does_not(self, topic: str) -> None:
        assert not relay.is_relayed(topic)

    def test_the_agent_command_channel_is_not_echoed_back(self) -> None:
        # Agents are controlled over `agents/{id}/commands`. Echoing it tells
        # every open browser what every other one is doing, for no feature.
        assert not relay.is_relayed("agents/a1/commands")

    def test_an_llm_request_never_reaches_a_browser(self) -> None:
        # These carry prompts, which can hold anything a user typed.
        assert not relay.is_relayed("main/llm_request")


@pytest.fixture(name="sent")
def sent_fixture() -> Iterator[list[dict[str, Any]]]:
    saved_registry, saved_reset = runtime.registry, runtime.hard_resetting
    runtime.registry = None
    runtime.hard_resetting = False
    frames: list[dict[str, Any]] = []

    async def _record(msg: dict[str, Any]) -> None:
        frames.append(msg)

    with patch.object(mqtt.ws, "broadcast", new=_record):
        yield frames

    runtime.registry, runtime.hard_resetting = saved_registry, saved_reset


def _relayed(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in frames if f.get("type") == "server_event"]


class TestTheFilterIsApplied:
    async def test_a_consumed_topic_still_reaches_the_browser(
        self, sent: list[dict[str, Any]]
    ) -> None:
        await mqtt.handle_message("agents/a1/alert", json.dumps({"message": "hot"}))

        assert _relayed(sent)

    async def test_an_unread_topic_does_not(self, sent: list[dict[str, Any]]) -> None:
        await mqtt.handle_message("agents/a1/llm_request", json.dumps({"prompt": "secret"}))

        assert not _relayed(sent)

    async def test_state_is_still_updated_for_a_topic_that_is_not_relayed(
        self, sent: list[dict[str, Any]]
    ) -> None:
        # ⚠ Only the raw echo is filtered. Dropping the parse too would stop the
        # dashboard updating for anything not on the list, which is a far bigger
        # change than the one intended.
        with patch.object(mqtt.events, "parse_topic") as parse:
            await mqtt.handle_message("agents/a1/llm_request", json.dumps({"prompt": "x"}))

        parse.assert_called_once()
