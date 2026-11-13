# SPDX-License-Identifier: Apache-2.0
"""An install's namespace: which values it may take, and what it does to the topics.

The namespace is read once, at import, and many topics are constants built
then. So what a namespace does is looked at in a fresh interpreter started with
``WACTORZ_NAMESPACE`` set, which imports everything the way a process does.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from wactorz import app as app_module
from wactorz.agents.main import manifests as main_manifests
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.core import node_env
from wactorz.core.topics import OWN_ROOTS, WORLD_ROOTS, namespace_error
from wactorz.errors import StartupError
from wactorz.node import cli as node_cli
from wactorz.node import runner as node_runner
from wactorz.node.runner import NodeRunner

#: How long a fresh interpreter may take to import the package and answer.
#: Generous: a loaded CI runner is slow, and the bug guarded against is a hang.
FRESH_IMPORT_TIMEOUT_S = 120.0

#: Run in the fresh interpreter: what the topics come to, as JSON.
PROBE = """
import json
from wactorz.core import topics, wiring
from wactorz.core.agent_directory import MANIFESTS
from wactorz.agents.main.llm_bridge import REQUEST_TOPIC
print(json.dumps({
    "namespace": topics.NAMESPACE,
    "refusal": topics.NAMESPACE_REFUSAL,
    "agent": topics.agent_topic("a1", "status"),
    "node": topics.node_topic("rpi", "spawn"),
    "task": topics.task_topic("rpi", "counter"),
    "manifests": MANIFESTS,
    "request": REQUEST_TOPIC,
    "shadow": topics.shadow_topic("anomalies/imu"),
    "shadow_of_own": topics.shadow_topic(topics.agent_topic("a1", "result")),
    "parts": topics.own_topic_parts(topics.agent_topic("a1", "manifest")),
    "bare_parts": topics.own_topic_parts("agents/a1/manifest"),
    "owner": topics.owner_of(topics.node_topic("rpi", "heartbeat"), topics.NODES),
    "wire": [topics.wire_topic(t) for t in (
        "agents/a1/answer", "agents/+/data/#", "sensors/air", "lab/agents/a1/answer",
        "+/agents/#", "#", "agents",
    )],
    "display": [topics.display_topic(t) for t in (
        "lab/agents/a1/answer", "lab/temperature", "sensors/air", "agents/a1/answer",
    )],
    "root_of": [wiring.root_of(t) for t in (topics.shadow_topic("x/y"), "sensors/imu")],
}))
"""


def _probe(namespace: str) -> dict[str, Any]:
    env = {**os.environ, "WACTORZ_NAMESPACE": namespace}
    done = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=FRESH_IMPORT_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


class TestWhatANamespaceMayBe:
    @pytest.mark.parametrize("value", ["", "lab", "house-2", "dev_1", "Λάμπα"])
    def test_one_plain_level_is_taken(self, value: str) -> None:
        assert namespace_error(value) is None

    @pytest.mark.parametrize(
        "value", ["my house", "lab\t", "a\nb", "a\x00b", "a\x7fb", "a/b", "+", "lab#", "$lab"]
    )
    def test_anything_that_breaks_a_topic_or_an_access_rule_is_refused(self, value: str) -> None:
        problem = namespace_error(value)
        assert problem is not None
        assert "WACTORZ_NAMESPACE" in problem

    @pytest.mark.parametrize("value", [*OWN_ROOTS, *WORLD_ROOTS])
    def test_a_roots_name_is_refused(self, value: str) -> None:
        assert namespace_error(value) is not None


class TestWithNoNamespace:
    def test_every_topic_is_todays(self) -> None:
        seen = _probe("")
        assert seen["namespace"] == "" and seen["refusal"] is None
        assert seen["agent"] == "agents/a1/status"
        assert seen["manifests"] == "agents/+/manifest"
        assert seen["request"] == "main/llm_request"
        assert seen["shadow"] == "shadow/anomalies/imu"
        assert seen["wire"][:3] == ["agents/a1/answer", "agents/+/data/#", "sensors/air"]
        assert seen["display"][3] == "agents/a1/answer"

    def test_a_value_of_only_whitespace_or_quotes_is_none(self) -> None:
        assert _probe('  "" ')["namespace"] == ""


@pytest.fixture(scope="module")
def seen() -> dict[str, Any]:
    """What the topics come to under the namespace ``lab``, probed once for the module."""
    return _probe("lab")


class TestWithANamespace:
    def test_own_topics_go_under_it(self, seen: dict[str, Any]) -> None:
        assert seen["namespace"] == "lab" and seen["refusal"] is None
        assert seen["agent"] == "lab/agents/a1/status"
        assert seen["node"] == "lab/nodes/rpi/spawn"
        assert seen["task"] == "lab/agents/at/rpi/counter/task"

    def test_topics_built_at_import_go_under_it_too(self, seen: dict[str, Any]) -> None:
        assert seen["manifests"] == "lab/agents/+/manifest"
        assert seen["request"] == "lab/main/llm_request"

    def test_a_shadow_goes_under_it_and_keeps_the_topic_inside_bare(
        self, seen: dict[str, Any]
    ) -> None:
        assert seen["shadow"] == "lab/shadow/anomalies/imu"
        assert seen["shadow_of_own"] == "lab/shadow/agents/a1/result"

    def test_own_topics_read_back_root_first_and_bare_ones_are_not_ours(
        self, seen: dict[str, Any]
    ) -> None:
        assert seen["parts"] == ["agents", "a1", "manifest"]
        assert seen["bare_parts"] is None
        assert seen["owner"] == "rpi"

    def test_wire_prefixes_only_a_bare_own_topic(self, seen: dict[str, Any]) -> None:
        assert seen["wire"] == [
            "lab/agents/a1/answer",
            "lab/agents/+/data/#",
            "sensors/air",
            "lab/agents/a1/answer",
            "+/agents/#",
            "#",
            "agents",
        ]

    def test_display_strips_only_an_own_topic(self, seen: dict[str, Any]) -> None:
        # `lab/temperature` is a world topic whose first level is the namespace.
        assert seen["display"] == [
            "agents/a1/answer",
            "lab/temperature",
            "sensors/air",
            "agents/a1/answer",
        ]

    def test_the_wiring_reads_a_root_through_the_namespace(self, seen: dict[str, Any]) -> None:
        assert seen["root_of"] == ["shadow", "sensors"]


class TestRefusedAtStart:
    REFUSAL = "WACTORZ_NAMESPACE='my house' contains whitespace or a control character"

    async def test_main_will_not_start_on_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app_module, "NAMESPACE_REFUSAL", self.REFUSAL)

        with pytest.raises(StartupError, match=r"my house.*one plain word"):
            app_module._check_startable(argparse.Namespace(reload=False), handle_signals=False)

    def test_main_names_nothing_when_there_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app_module, "NAMESPACE_REFUSAL", None)

        assert app_module.namespace_refusal() == ""

    def test_a_node_will_not_start_on_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(node_cli, "NAMESPACE_REFUSAL", self.REFUSAL)

        with pytest.raises(SystemExit) as exited:
            node_cli.check_startable("rpi")
        # The status the node's systemd unit does not restart on.
        assert exited.value.code == 2


class TestANodeIsTold:
    def _env(self) -> str:
        return node_env.node_env_text("edge", "broker", 1883, username="", password="")

    def test_its_env_names_mains_namespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(node_env, "NAMESPACE", "lab")

        assert "WACTORZ_NAMESPACE=lab\n" in self._env()

    def test_with_none_its_env_says_nothing_new(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(node_env, "NAMESPACE", "")

        assert "WACTORZ_NAMESPACE" not in self._env()

    async def test_its_heartbeat_says_which_namespace_it_is_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(node_runner, "NAMESPACE", "lab")
        runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))
        runner._running = True
        published: list[tuple[str, Any]] = []

        async def publish(topic: str, data: Any, retain: bool = False) -> None:
            published.append((topic, data))
            runner._running = False

        runner.publish = publish  # type: ignore[method-assign]

        await asyncio.wait_for(runner._node_heartbeat_loop(interval=0), timeout=10.0)

        assert published[0][1]["namespace"] == "lab"


class TestWithANamespaceThatCannotBe:
    def test_it_is_refused_and_the_topics_stay_bare_until_the_refusal(self) -> None:
        seen = _probe("my house")
        assert seen["namespace"] == ""
        assert seen["refusal"] is not None and "whitespace" in seen["refusal"]
        assert seen["agent"] == "agents/a1/status"


class TestWhatAnEarlierNamespaceLeftBehind:
    """Main's own manifest from before the namespace, retained: said once, cleared never."""

    def test_it_is_looked_for_only_under_a_namespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(main_manifests, "NAMESPACE", "")
        assert main_manifests.leftover_manifest_topic("m1") == ""

        monkeypatch.setattr(main_manifests, "NAMESPACE", "lab")
        assert main_manifests.leftover_manifest_topic("m1") == "agents/m1/manifest"

    def test_it_is_said_once_and_a_cleared_one_not_at_all(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = ManifestRegistry()

        with caplog.at_level("WARNING"):
            registry.say_leftover("agents/m1/manifest", b"")
            registry.say_leftover("agents/m1/manifest", b"{}")
            registry.say_leftover("agents/m1/manifest", b"{}")

        said = [r for r in caplog.records if "still retained" in r.getMessage()]
        assert len(said) == 1
        assert "agents/m1/manifest" in said[0].getMessage()
