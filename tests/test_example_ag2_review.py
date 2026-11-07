# SPDX-License-Identifier: Apache-2.0
"""The ag2_review example: an AG2 writer-critic conversation as an agent.

What it needs that is not installed for the suite is stood in for.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.examples import load
from wactorz.agents.function_agent import spec_of


class TestTheAG2Example:
    async def test_without_a_model_the_draft_comes_back_after_one_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("ag2")
        review = load("ag2_review").review
        actor = spec_of(review).build(persistence_dir=str(tmp_path))  # pyright: ignore[reportOptionalMemberAccess]
        said: list[str] = []

        async def log(message: str, level: str = "info") -> None:
            said.append(level)

        monkeypatch.setattr(actor, "log", log)

        result = await actor.call({"id": "d1", "text": "We is pleased to announce the pump."})

        assert result is not None
        assert result["id"] == "d1" and result["turns"] == 2
        assert result["cost_usd"] == 0.0, "no model, no spend"
        assert said == ["warning"], "and it says so on the feed"
        assert actor.recall("drafts_total") == 1
        assert await actor.call({"id": "d2", "text": "  "}) is None

    async def test_the_critic_sends_the_writer_back_until_it_approves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AG2's test client stands in for the model, with the turns scripted."""
        pytest.importorskip("ag2")
        from ag2 import Agent  # pyright: ignore[reportMissingImports]
        from ag2.testing import TestConfig  # pyright: ignore[reportMissingImports]

        module = load("ag2_review")

        def scripted(config: Any) -> Any:
            assert config == "scripted"
            writer = Agent(
                "writer", "You improve drafts.", config=TestConfig("Version one.", "Version two.")
            )
            critic = Agent(
                "critic",
                "You review.",
                config=TestConfig("Too long.", f"Good now. {module.APPROVED}"),
            )
            return writer, critic

        monkeypatch.setattr(module, "make_agents", scripted)
        actor = spec_of(module.review).build(  # pyright: ignore[reportOptionalMemberAccess]
            persistence_dir=str(tmp_path), options={"config": "scripted"}
        )

        result = await actor.call({"id": "d3", "text": "Draft."})

        assert result is not None
        assert result["text"] == "Version two."
        assert result["turns"] == 4, "write, critique, rewrite, approve"
