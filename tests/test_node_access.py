# SPDX-License-Identifier: Apache-2.0
"""A topic the broker's access list refuses a node is said, not lost in silence.

The broker answers nothing to a refused publish, so the agent runs and its data
never arrives. Main reads what a node's agent uses from its manifest and holds
it against the same rules the access list is written from; `tests/broker` asks a
real broker whether those two agree.
"""

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from wactorz import broker_certificates, config
from wactorz.agents.main import node_access
from wactorz.agents.main.hosts import ManifestHost
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.node_access import Refused, RefusedTopics, refused_topics
from wactorz.config import DeployTarget
from wactorz.core import broker_accounts
from wactorz.core.broker_accounts import NODE_TOPICS, acl_text, filter_covers, node_may, node_rules
from wactorz.core.topics import agent_topic, main_topic, node_topic, wire_topic

NODE = "rpi"


class TestWhatAFilterCovers:
    @pytest.mark.parametrize(
        ("rule", "topic", "covered"),
        [
            ("sensors/#", "sensors/kitchen/temp", True),
            ("sensors/#", "sensors", True),
            ("sensors/#", "sensor", False),
            ("sensors/+", "sensors/kitchen", True),
            ("sensors/+", "sensors/kitchen/temp", False),
            ("sensors/+", "sensors", False),
            ("models/+/drift", "models/air/drift", True),
            ("models/+/drift", "models/air/active", False),
            ("sensors/kitchen", "sensors/kitchen", True),
            # A filter is within another only when everything it matches is.
            ("sensors/#", "sensors/+/temp", True),
            ("sensors/#", "sensors/#", True),
            ("sensors/+", "sensors/#", False),
            ("sensors/kitchen", "sensors/+", False),
            ("sensors/+/temp", "sensors/+/temp", True),
            ("#", "anything/at/all", True),
        ],
    )
    def test_a_topic_or_a_filter_within_a_rule(self, rule: str, topic: str, covered: bool) -> None:
        assert filter_covers(rule, topic) is covered


class TestWhatANodeMayUse:
    @pytest.mark.parametrize(
        ("use", "topic", "allowed"),
        [
            ("publish", "sensors/imu", True),
            ("subscribe", "sensors/#", True),
            ("publish", "anomalies/imu", False),
            ("subscribe", "anomalies/#", False),
            # Read-only data: heard, not written.
            ("subscribe", "homeassistant/state_changes/#", True),
            ("publish", "homeassistant/state_changes/light.kitchen", False),
            ("publish", node_topic(NODE, "heartbeat"), True),
            ("publish", node_topic("other", "spawn"), False),
            ("subscribe", node_topic("other", "heartbeat"), False),
            ("publish", agent_topic("abc", "status"), True),
            # The deny beats the allow around it.
            ("publish", agent_topic("abc", "commands"), False),
            ("subscribe", agent_topic("abc", "commands"), False),
            # Written, never read.
            ("publish", main_topic("reply", "abc"), True),
            ("subscribe", main_topic("reply", "abc"), False),
            ("subscribe", "$share/workers/sensors/#", True),
            ("subscribe", "$share/workers/anomalies/#", False),
            ("subscribe", "#", False),
        ],
    )
    def test_by_the_rules_the_access_list_is_written_from(
        self, use: Any, topic: str, allowed: bool
    ) -> None:
        assert node_may(NODE, use, topic) is allowed

    def test_what_the_setting_adds_is_allowed_as_it_says(self) -> None:
        added = [*NODE_TOPICS, *broker_accounts.parse_node_topics("anomalies/#, read:weather/#")]

        assert node_may(NODE, "publish", "anomalies/imu", added)
        assert node_may(NODE, "subscribe", "weather/today", added)
        assert not node_may(NODE, "publish", "weather/today", added)

    def test_every_rule_is_a_line_of_the_nodes_block_in_the_file(self) -> None:
        block = acl_text([NODE], ["server"]).split(f"user {NODE}\n")[1]
        lines = [line for line in block.splitlines() if line.startswith("topic ")]

        assert lines == [f"topic {access} {topic}" for access, topic in node_rules(NODE)]


def _configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **changes: Any) -> None:
    """A server that writes the access list, with `rpi` among its deploy targets."""
    settings: dict[str, Any] = {
        "node_accounts": True,
        "mqtt_broker_dir": str(tmp_path),
        "deploy_targets": [DeployTarget(name=NODE, host="rpi.local")],
        "node_topics": "",
        **changes,
    }
    changed = dataclasses.replace(config.CONFIG, **settings)
    monkeypatch.setattr(config, "CONFIG", changed)
    monkeypatch.setattr(broker_certificates, "CONFIG", changed)


class TestWhereTheListIsInForce:
    def test_for_a_deploy_target_when_this_server_writes_the_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configured(monkeypatch, tmp_path)

        assert broker_certificates.node_is_under_access_list(NODE)
        assert not broker_certificates.node_is_under_access_list("elsewhere")
        assert not broker_certificates.node_is_under_access_list("")

    @pytest.mark.parametrize("off", [{"node_accounts": False}, {"mqtt_broker_dir": ""}])
    def test_not_where_this_server_writes_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, off: dict[str, Any]
    ) -> None:
        _configured(monkeypatch, tmp_path, **off)

        assert not broker_certificates.node_is_under_access_list(NODE)

    def test_not_for_a_node_whose_name_no_account_can_have(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configured(
            monkeypatch, tmp_path, deploy_targets=[DeployTarget(name="my pi", host="rpi.local")]
        )

        assert not broker_certificates.node_is_under_access_list("my pi")


class _Host:
    """Main, as far as saying something in chat goes."""

    #: No agents of its own: every manifest is another host's.
    _registry = None

    def __init__(self) -> None:
        self.notices: list[dict[str, Any]] = []

    def _queue_notification(self, notice: dict[str, Any]) -> None:
        self.notices.append(notice)


class TestWhatIsSaid:
    def test_the_refused_among_what_an_agent_uses(self) -> None:
        refused = refused_topics(
            NODE, ["sensors/imu", "anomalies/imu", ""], ["sensors/#", "zigbee2mqtt/#"], NODE_TOPICS
        )

        assert refused == [
            Refused("publish", "anomalies/imu"),
            Refused("subscribe", "zigbee2mqtt/#"),
        ]

    def test_each_refused_topic_once_with_what_to_add(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _configured(monkeypatch, tmp_path)
        host, said = _Host(), RefusedTopics()

        with caplog.at_level(logging.WARNING, logger=node_access.__name__):
            for _ in range(2):
                said.say(
                    host, "imu-watch", NODE, ["anomalies/imu", "sensors/imu"], ["zigbee2mqtt/#"]
                )

        assert [n["severity"] for n in host.notices] == ["warning", "warning"]
        publish, subscribe = (n["message"] for n in host.notices)
        assert "Agent 'imu-watch' on node 'rpi' publishes to `anomalies/imu`" in publish
        assert "add `anomalies/imu` to WACTORZ_NODE_TOPICS" in publish
        assert "subscribes to `zigbee2mqtt/#`" in subscribe
        assert "add `read:zigbee2mqtt/#` to WACTORZ_NODE_TOPICS" in subscribe
        assert caplog.text.count("anomalies/imu") == 2  # the topic and what to add, in one line
        assert len({n["about"] for n in host.notices}) == 2

    def test_a_later_topic_of_the_same_agent_is_said_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configured(monkeypatch, tmp_path)
        host, said = _Host(), RefusedTopics()

        said.say(host, "imu-watch", NODE, ["anomalies/imu"], [])
        said.say(host, "imu-watch", NODE, ["anomalies/imu", "alarms/imu"], [])

        assert ["alarms/imu" in n["message"] for n in host.notices] == [False, True]

    def test_a_topic_every_node_is_kept_from_is_not_one_to_add(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configured(monkeypatch, tmp_path)
        host = _Host()

        RefusedTopics().say(host, "stopper", NODE, [agent_topic("abc", "commands")], [])

        (notice,) = host.notices
        assert "every node is kept from" in notice["message"]
        assert "WACTORZ_NODE_TOPICS" not in notice["message"]

    def test_nothing_once_the_setting_allows_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configured(monkeypatch, tmp_path, node_topics="anomalies/#")
        host = _Host()

        RefusedTopics().say(host, "imu-watch", NODE, ["anomalies/imu"], [])

        assert host.notices == []

    @pytest.mark.parametrize(
        "changes", [{"node_accounts": False}, {"node_topics": "agents/#"}, {"deploy_targets": []}]
    )
    def test_nothing_where_main_cannot_know_the_rules(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, changes: dict[str, Any]
    ) -> None:
        # No list written here, a setting that could not be read, or a node with no account.
        _configured(monkeypatch, tmp_path, **changes)
        host = _Host()

        RefusedTopics().say(host, "imu-watch", NODE, ["anomalies/imu"], [])

        assert host.notices == []


class TestFromAManifest:
    async def _received(self, host: _Host, manifest: dict[str, Any]) -> ManifestRegistry:
        registry = ManifestRegistry(cast(ManifestHost, host))
        # Not off the broker, so there is no signature to weigh: the manifest is taken as it is.
        await registry.receive_manifest(
            agent_topic("abc123", "manifest"), json.dumps(manifest).encode()
        )
        return registry

    async def test_a_nodes_agent_using_a_refused_topic_is_said(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _configured(monkeypatch, tmp_path)
        host = _Host()

        await self._received(
            host,
            {
                "name": "imu-watch",
                "actor_id": "abc123",
                "node": NODE,
                "publishes": ["anomalies/imu", wire_topic("agents/abc123/data")],
                "subscribes": ["sensors/imu"],
            },
        )

        assert ["anomalies/imu" in n["message"] for n in host.notices] == [True]

    @pytest.mark.parametrize("node", [None, "main", "elsewhere"])
    async def test_an_agent_on_main_or_on_a_node_without_an_account_is_not(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, node: str | None
    ) -> None:
        _configured(monkeypatch, tmp_path)
        host = _Host()

        await self._received(
            host,
            {
                "name": "imu-watch",
                "actor_id": "abc123",
                "node": node,
                "publishes": ["anomalies/imu"],
            },
        )

        assert host.notices == []
