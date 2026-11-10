# SPDX-License-Identifier: Apache-2.0
"""The topics Wactorz owns, built and read in one place.

The helpers in `wactorz.core.topics` are the only spelling of ``agents/``,
``nodes/``, ``main/``, ``system/``, ``models/`` and ``pipelines/``. The literal
topics below are the wire format as it stands: with nothing configured, every
helper gives exactly these. The last class keeps it that way, by refusing a
topic under one of those roots spelled out anywhere else in the package or the
examples.
"""

import ast
from pathlib import Path

import pytest

from wactorz.core import topics
from wactorz.core.topics import (
    OWN_ROOTS,
    TASK_BY_NAME,
    TASK_BY_NODE,
    agent_topic,
    main_topic,
    model_topic,
    node_topic,
    own_topic_parts,
    owner_of,
    pipeline_topic,
    root_prefix,
    system_topic,
)

ROOT = Path(__file__).resolve().parents[1]


class TestBuilding:
    def test_each_root_spells_todays_topics(self) -> None:
        assert agent_topic("a1", "heartbeat") == "agents/a1/heartbeat"
        assert node_topic("rpi", "spawn") == "nodes/rpi/spawn"
        assert main_topic("llm_request") == "main/llm_request"
        assert system_topic("health") == "system/health"
        assert model_topic("air") == "models/air"
        assert model_topic("air", "drift") == "models/air/drift"
        assert pipeline_topic("imu-watch", "tick") == "pipelines/imu-watch/tick"

    def test_deeper_levels_and_wildcards_are_more_levels(self) -> None:
        assert agent_topic("+", "data", "#") == "agents/+/data/#"
        assert agent_topic("#") == "agents/#"
        assert node_topic("rpi", "blob_return", "abc") == "nodes/rpi/blob_return/abc"
        assert main_topic("reply", "io-gateway", "k1") == "main/reply/io-gateway/k1"
        assert model_topic("+", "+") == "models/+/+"

    def test_an_empty_last_level_gives_a_prefix_to_match_under(self) -> None:
        assert node_topic("rpi", "reply", "") == "nodes/rpi/reply/"

    def test_the_task_families_sit_under_agents(self) -> None:
        assert TASK_BY_NODE == "agents/at"
        assert TASK_BY_NAME == "agents/by-name"

    @pytest.mark.parametrize("root", OWN_ROOTS)
    def test_a_root_prefix_ends_in_a_slash(self, root: str) -> None:
        assert root_prefix(root) == f"{root}/"


class TestReading:
    @pytest.mark.parametrize(
        ("topic", "parts"),
        [
            ("agents/a1/manifest", ["agents", "a1", "manifest"]),
            ("nodes/rpi/heartbeat", ["nodes", "rpi", "heartbeat"]),
            ("system/health", ["system", "health"]),
            ("models/air/active", ["models", "air", "active"]),
            ("agents/at/rpi/counter/task", ["agents", "at", "rpi", "counter", "task"]),
        ],
    )
    def test_an_own_topic_reads_back_root_first(self, topic: str, parts: list[str]) -> None:
        assert own_topic_parts(topic) == parts

    @pytest.mark.parametrize(
        "topic",
        ["sensors/air/kitchen", "home/presence/hall", "homeassistant/state_changes/x", "$SYS/x"],
    )
    def test_a_world_topic_is_not_ours(self, topic: str) -> None:
        assert own_topic_parts(topic) is None

    @pytest.mark.parametrize("topic", ["agents", "agentsx/a1/logs", "nodesfoo/rpi/spawn"])
    def test_a_root_without_its_slash_is_not_ours(self, topic: str) -> None:
        assert own_topic_parts(topic) is None

    def test_whose_topic_it_is(self) -> None:
        assert owner_of("nodes/rpi/heartbeat", topics.NODES) == "rpi"
        assert owner_of("agents/a1/manifest", topics.AGENTS) == "a1"

    @pytest.mark.parametrize(
        ("topic", "root"),
        [
            ("agents/a1/manifest", "nodes"),
            ("sensors/rpi/x", "nodes"),
            ("nodesx/rpi/spawn", "nodes"),
            ("system", "system"),
        ],
    )
    def test_no_owner_outside_its_root(self, topic: str, root: str) -> None:
        assert owner_of(topic, root) == ""

    def test_every_namespace_gives_two_filters_that_never_meet(self) -> None:
        assert topics.every_namespace(topics.NODES, "+", "heartbeat") == (
            "nodes/+/heartbeat",
            "+/nodes/+/heartbeat",
        )

    @pytest.mark.parametrize(
        ("topic", "sent_under"),
        [
            ("nodes/rpi/heartbeat", ("", "rpi")),
            ("lab/nodes/rpi/heartbeat", ("lab", "rpi")),
            ("sensors/rpi/heartbeat", None),
            ("nodes", None),
        ],
    )
    def test_which_namespace_a_topic_came_under(
        self, topic: str, sent_under: tuple[str, str] | None
    ) -> None:
        assert topics.namespace_of(topic, topics.NODES) == sent_under

    def test_what_is_built_reads_back(self) -> None:
        assert own_topic_parts(node_topic("rpi", "stop")) == ["nodes", "rpi", "stop"]


# ── Nothing else spells them ───────────────────────────────────────────────────

#: Where a topic is spelled: the package and the examples. Tests and e2e spell
#: the wire format on purpose, as a check on what the helpers give.
_SCANNED = ("wactorz", "examples")

#: The one module that spells the roots.
_SPELLER = ROOT / "wactorz" / "core" / "topics.py"


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _head(node: ast.expr) -> str | None:
    """The text a string expression starts with, or None if it is no string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _spelled_by_hand(source: str, where: str) -> list[str]:
    tree = ast.parse(source)
    skip = _docstrings(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            skip.update(id(value) for value in node.values)
    prefixes = tuple(f"{root}/" for root in OWN_ROOTS)
    found: list[str] = []
    for node in ast.walk(tree):
        if id(node) in skip or not isinstance(node, ast.expr):
            continue
        head = _head(node)
        if head is not None and head.startswith(prefixes):
            found.append(f"{where}:{node.lineno}: {head!r}")
    return found


#: Readers that split a topic level by level on purpose, and why that is no
#: positional reading of an own topic: (file, function).
_GENERIC_READERS = {
    # Matches a filter against a topic, level for level, whatever the root.
    ("wactorz/core/topic_bus.py", "topic_matches"),
    # A subscription filter's levels up to its first wildcard, as a recorder's source.
    ("wactorz/agents/recorder_agent.py", "source_from_topic"),
    # Words in a user's task that look like topics, not topics that arrived.
    ("wactorz/agents/planner/context.py", "topics_named_in"),
    # A filter a person configured for nodes, checked level by level against the fences.
    ("wactorz/core/broker_accounts.py", "_reaches_a_fence"),
}


def _functions_of(tree: ast.AST) -> dict[int, str]:
    """Each node's innermost enclosing function name, by node id; module level is absent.

    `ast.walk` reaches an outer function before the ones inside it, so a later
    write is the closer function.
    """
    owner: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if inner is not node:
                    owner[id(inner)] = node.name
    return owner


def _is_slash_split(node: ast.AST) -> bool:
    """``<something named *topic*>.split("/")``."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "split"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "/"
    ):
        return False
    receiver = node.func.value
    name = receiver.id if isinstance(receiver, ast.Name) else getattr(receiver, "attr", "")
    return "topic" in name.lower()


def _compares_a_root(node: ast.AST) -> bool:
    """``parts[0] == "agents"``, ``root != "nodes"``: a level checked against a bare root."""
    if not isinstance(node, ast.Compare):
        return False
    sides = [node.left, *node.comparators]
    names_a_root = any(isinstance(s, ast.Constant) and s.value in OWN_ROOTS for s in sides)
    reads_a_level = any(
        isinstance(s, ast.Subscript) or (isinstance(s, ast.Name) and s.id == "root") for s in sides
    )
    return names_a_root and reads_a_level


def _read_by_hand(source: str, where: str) -> list[str]:
    """Where ``source`` reads an own topic by position instead of through `own_topic_parts`."""
    tree = ast.parse(source)
    functions = _functions_of(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if (where, functions.get(id(node), "")) in _GENERIC_READERS:
            continue
        if _is_slash_split(node) or _compares_a_root(node):
            found.append(f"{where}:{getattr(node, 'lineno', 0)}: {ast.unparse(node)}")
    return found


def _sources() -> list[Path]:
    return sorted(p for folder in _SCANNED for p in (ROOT / folder).rglob("*.py") if p != _SPELLER)


class TestNothingElseSpellsThem:
    def test_the_scan_reads_the_package_and_the_examples(self) -> None:
        sources = _sources()
        assert any(p.is_relative_to(ROOT / "wactorz" / "node") for p in sources)
        assert any(p.is_relative_to(ROOT / "examples") for p in sources)

    def test_the_scan_sees_a_topic_spelled_by_hand(self) -> None:
        source = 'A = "agents/+/manifest"\nB = f"nodes/{n}/spawn"\nC = "sensors/air"\n'
        found = _spelled_by_hand(source, "x.py")
        assert found == ["x.py:1: 'agents/+/manifest'", "x.py:2: 'nodes/'"]

    def test_no_module_spells_an_own_root(self) -> None:
        found = [
            line
            for path in _sources()
            for line in _spelled_by_hand(
                path.read_text(encoding="utf-8"), str(path.relative_to(ROOT))
            )
        ]
        assert found == [], "build these with the helpers in wactorz.core.topics:\n" + "\n".join(
            found
        )

    def test_the_scan_sees_a_topic_read_by_hand(self) -> None:
        source = (
            "def f(topic, parts, root, name):\n"
            '    a = topic.split("/")\n'
            '    b = parts[0] == "agents"\n'
            '    c = root != "nodes"\n'
            '    d = name == "main"\n'
            '    e = path.split("/")\n'
        )
        found = _read_by_hand(source, "x.py")
        assert [line.split(":")[1] for line in found] == ["2", "3", "4"]

    def test_a_generic_reader_is_let_through_by_name(self) -> None:
        source = 'def topic_matches(pattern, topic):\n    return topic.split("/")\n'
        assert _read_by_hand(source, "wactorz/core/topic_bus.py") == []
        assert _read_by_hand(source, "wactorz/elsewhere.py") != []

    def test_no_module_reads_an_own_topic_by_position(self) -> None:
        found = [
            line
            for path in _sources()
            for line in _read_by_hand(
                path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix()
            )
        ]
        assert found == [], "read these with own_topic_parts or owner_of:\n" + "\n".join(found)

    def test_the_roots_are_spelled_once(self) -> None:
        assert topics.OWN_ROOTS == (
            "agents",
            "nodes",
            "main",
            "system",
            "models",
            "pipelines",
            "shadow",
        )
