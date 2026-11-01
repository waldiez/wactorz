"""A topic the request names is sampled, though no agent declares it.

A device or a script publishing straight to the broker declares no contract,
so its topic is sampled only because the request names it; without a sample
the model writes field names it imagines, and the agent it writes never fires.
"""

from pathlib import Path
from typing import Any

import pytest

from tests import test_planner_pipeline_context as planner_context
from wactorz.agents.planner import context as context_mod
from wactorz.agents.planner.context import topics_named_in
from wactorz.agents.planner.pipeline import with_named_samples


@pytest.mark.parametrize(
    ("task", "topics"),
    [
        ("flag odd readings on sensors/imu/# please", ["sensors/imu/#"]),
        ("watch `sensors/imu/left-wrist`.", ["sensors/imu/left-wrist"]),
        ("from home/+/temperature and home/+/temperature", ["home/+/temperature"]),
        ("a/b, c/d, e/f and g/h", ["a/b", "c/d", "e/f"]),
        ("run examples/imu_anomaly/publish_imu.py", []),
        ("see https://example.com/a/b", []),
        (r"C:\Users\me\model.npz", []),
        ("sensors/#/imu", []),
        ("sensors//imu", []),
        ("the door opens", []),
    ],
    ids=[
        "wildcard",
        "quoted",
        "repeated",
        "at-most-three",
        "file",
        "url",
        "windows-path",
        "inner-hash",
        "empty-segment",
        "none",
    ],
)
def test_the_topics_a_request_names(task: str, topics: list[str]) -> None:
    assert topics_named_in(task) == topics


class _Message:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class _Client:
    """A broker connection that delivers the messages it is given."""

    def __init__(self, messages: list[_Message]) -> None:
        self._messages = messages
        self.subscribed: list[str] = []

    def __call__(self, _host: str, _port: int, **_kwargs: Any) -> "_Client":
        return self

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def subscribe(self, topic: str, **_kwargs: Any) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        return self._stream()

    async def _stream(self) -> Any:
        for message in self._messages:
            yield message


class TestSamplingThem:
    async def test_a_wildcard_is_sampled_on_the_topic_that_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _Client([_Message("sensors/imu/left-wrist", b'{"ax": 0.1, "ay": 0, "az": 1}')])
        monkeypatch.setattr(context_mod, "mqtt_client", client)
        planner = planner_context._planner(tmp_path)

        (line,) = await planner._sample_named_topics("flag odd readings on sensors/imu/#")

        assert client.subscribed == ["sensors/imu/#"]
        assert line.startswith("  Topic: sensors/imu/left-wrist  (named in the request)")
        assert "Fields: {'ax': 'float', 'ay': 'int', 'az': 'int'}" in line

    async def test_a_request_naming_none_listens_to_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _Client([])
        monkeypatch.setattr(context_mod, "mqtt_client", client)
        planner = planner_context._planner(tmp_path)

        assert await planner._sample_named_topics("when the door opens") == []
        assert client.subscribed == []

    async def test_a_broker_that_refuses_gives_none(self, tmp_path: Path) -> None:
        # The suite-wide fixture refuses every broker connection.
        planner = planner_context._planner(tmp_path)

        assert await planner._sample_named_topics("watch sensors/imu/#") == []


LINE = "  Topic: sensors/imu/left-wrist  (named in the request)\n    Fields: {'ax': 'float'}"


class TestBesideTheDeclaredOnes:
    def test_alone_they_get_the_heading(self) -> None:
        section = with_named_samples("", [LINE])

        assert section.startswith("LIVE TOPIC SAMPLES")
        assert section.endswith(LINE)

    def test_one_already_sampled_from_its_contract_is_not_repeated(self) -> None:
        declared = "LIVE TOPIC SAMPLES:\n  Topic: sensors/imu/left-wrist  (published by imu)"

        assert with_named_samples(declared, [LINE]) == declared

    def test_others_are_added_after_them(self) -> None:
        declared = "LIVE TOPIC SAMPLES:\n  Topic: home/door  (published by door)"

        assert with_named_samples(declared, [LINE]) == f"{declared}\n{LINE}"

    def test_none_leaves_the_section_as_it_was(self) -> None:
        assert with_named_samples("", []) == ""

    async def test_they_reach_the_planners_prompt(self, tmp_path: Path) -> None:
        decompose = planner_context.TestDecomposePipeline
        planner = planner_context._planner(tmp_path, script={"sensors/imu/#": decompose.PLAN})
        decompose._grounded(planner)

        async def _named(task: str) -> list[str]:
            return [LINE]

        planner._sample_named_topics = _named  # pyright: ignore[reportAttributeAccessIssue]

        await planner._decompose_pipeline("flag odd readings on sensors/imu/#", [])

        prompt = planner_context._prompt(planner)
        assert "═══ LIVE TOPIC SAMPLES" in prompt
        assert LINE in prompt
