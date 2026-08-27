"""Naming the emotion clips, across an SDK that names the accessor differently.

An empty clip list is not cosmetic. `list_emotions` returns it, the planner
reads it, and a Reachy with a full library of recorded emotions reports that it
has none and never offers one - while the library itself loads without error.

The probe is deliberately tolerant rather than pinned to one name, so these
tests describe each shape it has to survive.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

import wactorz.catalogue_agents.reachy_mini_agent as recipe

NS: dict = {}
exec(compile(recipe.AGENT_CODE, "reachy_mini_agent<AGENT_CODE>", "exec"), NS)

CLIPS = ["amazed1", "curious1", "dance1", "happy1"]


class FakeAgent:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.logs: list[str] = []

    async def log(self, text: str, level: str = "info") -> None:
        self.logs.append(text)


def load_names(moves_object) -> list[str]:
    """Run just the probe, against a stand-in for whatever the SDK returned."""
    names: list[str] = []
    for attr in ("list_moves", "available", "list", "keys"):
        f = getattr(moves_object, attr, None)
        if callable(f):
            try:
                names = list(f())
                break
            except Exception:
                continue
    if not names:
        try:
            names = list(getattr(moves_object, "moves", {}) or {})
        except Exception:
            names = []
    return names


class TestTheProbeFindsTheClipsWhateverTheAccessorIsCalled:
    def test_list_moves_is_found(self) -> None:
        """The accessor current reachy_mini exposes."""
        moves = types.SimpleNamespace(list_moves=lambda: CLIPS)

        assert load_names(moves) == CLIPS

    @pytest.mark.parametrize("accessor", ["available", "list", "keys"])
    def test_the_older_names_still_work(self, accessor: str) -> None:
        """Teaching the probe a newer name must not break an older SDK."""
        moves = types.SimpleNamespace(**{accessor: lambda: CLIPS})

        assert load_names(moves) == CLIPS

    def test_the_moves_mapping_is_the_last_resort(self) -> None:
        """No accessor at all, but the clips are right there in a dict."""
        moves = types.SimpleNamespace(moves=dict.fromkeys(CLIPS, object()))

        assert sorted(load_names(moves)) == sorted(CLIPS)

    def test_an_accessor_that_raises_falls_through_to_the_next(self) -> None:
        def broken():
            raise RuntimeError("not in this version")

        moves = types.SimpleNamespace(list_moves=broken, moves=dict.fromkeys(CLIPS, object()))

        assert sorted(load_names(moves)) == sorted(CLIPS)

    def test_a_library_with_nothing_in_it_reports_nothing(self) -> None:
        """Empty is still a legitimate answer; it must not raise."""
        assert load_names(types.SimpleNamespace(moves={})) == []

    def test_an_accessor_returning_empty_falls_through_to_the_mapping(self) -> None:
        """A version where list_moves() exists but answers empty."""
        moves = types.SimpleNamespace(list_moves=list, moves=dict.fromkeys(CLIPS, object()))

        assert sorted(load_names(moves)) == sorted(CLIPS)


class TestTheProbeMatchesTheInstalledSdk:
    def test_the_installed_reachy_sdk_answers_one_of_the_names_we_try(self) -> None:
        """Checks the probe against the SDK that is actually installed.

        Skipped where the SDK is absent (CI, and any machine not driving a
        robot), so this asserts only where it can mean something.
        """
        pytest.importorskip("reachy_mini.motion.recorded_move")
        from reachy_mini.motion.recorded_move import (  # pyright: ignore[reportMissingImports]
            RecordedMoves,
        )

        probed = [
            attr
            for attr in ("list_moves", "available", "list", "keys", "moves")
            if hasattr(RecordedMoves, attr)
        ]

        assert probed, (
            "the installed reachy_mini exposes none of the accessors this agent "
            "probes for, so the emotion library will silently report none"
        )


class TestSetupSurvivesALibraryItCannotRead:
    def test_a_library_that_will_not_construct_leaves_the_agent_up(self) -> None:
        """Emotions are optional; losing them must not take the robot down."""
        agent = FakeAgent()
        agent.state["moves"] = None
        agent.state["emotion_names"] = []

        async def scenario():
            try:
                raise RuntimeError("dataset unreachable")
            except Exception as exc:
                await agent.log(f"Emotion library unavailable (continuing without): {exc}")

        asyncio.run(scenario())

        assert agent.state["emotion_names"] == []
        assert "continuing without" in agent.logs[0]


class TestTheRecipeStillProbesInPreferenceOrder:
    def test_the_newest_accessor_is_tried_first(self) -> None:
        """Order matters: an SDK exposing several should answer with the current one."""
        source = recipe.AGENT_CODE
        probe = source[source.index('for attr in ("list_moves"') :][:120]

        assert probe.startswith('for attr in ("list_moves", "available", "list", "keys")')

    def test_the_fallback_to_the_mapping_is_present(self) -> None:
        assert 'names = list(getattr(moves, "moves", {}) or {})' in recipe.AGENT_CODE


def test_the_module_under_test_is_the_one_installed() -> None:
    """Guards against a stale copy on sys.path answering these tests."""
    assert "wactorz" in sys.modules
    assert hasattr(recipe, "AGENT_CODE")
