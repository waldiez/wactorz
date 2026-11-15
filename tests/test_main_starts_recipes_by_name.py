# SPDX-License-Identifier: Apache-2.0
"""Asking main to start a catalogue recipe by name starts that recipe.

A first-time user types "spawn reachy mini", not ``@catalog spawn reachy-mini``.
The recipe is not running, so a model that sees only running agents reads the
request as one to write a new agent, and asks what its code should do. Two
things keep that from happening, and both are pinned here:

- A request that is nothing but a recipe's name goes to the catalog with no
  model call, whatever the spelling of the name.
- Any other request reaches the model with the recipes listed in its system
  prompt, and how to have the catalog start one.
"""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.manifests import ManifestRegistry
from wactorz.agents.main.routing import spawn_request_name
from wactorz.core.manifests import AgentManifest


def recipe(description: str = "", **over: Any) -> AgentManifest:
    """A manifest the catalog writes into main for one of its recipes."""
    return cast(
        AgentManifest,
        {"spawnable": True, "catalog": "catalog", "description": description, **over},
    )


RECIPES: dict[str, AgentManifest] = {
    "reachy-mini": recipe("Drives a Reachy Mini robot"),
    "anomaly-detector": recipe("Flags unusual sensor readings"),
    "manual-agent": recipe("Finds device manuals"),
}


class _Registry:
    """The running actors, by name."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self._by = {n: SimpleNamespace(name=n) for n in names}

    def find_by_name(self, name: str) -> SimpleNamespace | None:
        return self._by.get(name)

    def all_actors(self) -> list[SimpleNamespace]:
        return list(self._by.values())


class _Delegation:
    """Records what main hands on as a typed ``@mention``."""

    def __init__(self) -> None:
        self.mentions: list[str] = []

    async def route_mention(self, text: str) -> str:
        self.mentions.append(text)
        return "catalog: spawning"


class _Main:
    """A MainActor whose turn stops at the model: what reached it, and what went to the catalog."""

    def __init__(self, running: tuple[str, ...] = ("catalog",)) -> None:
        m = MainActor.__new__(MainActor)
        m.name = "main"
        m.actor_id = "main-under-test"
        m._registry = _Registry(running)  # pyright: ignore[reportAttributeAccessIssue]
        m.manifests = ManifestRegistry(m)
        m._agent_manifests = dict(RECIPES)
        self.delegation = _Delegation()
        m.delegation = self.delegation  # pyright: ignore[reportAttributeAccessIssue]
        self.classified: list[str] = []
        self.recorded: list[tuple[str, str]] = []

        m._drain_notifications = lambda: ""

        async def _no_plan(_text: str) -> str | None:
            return None

        m._handle_pending_plan_response = _no_plan  # pyright: ignore[reportAttributeAccessIssue]
        m._warn_if_pending_plan_collision = lambda _text: None  # pyright: ignore[reportAttributeAccessIssue]

        async def _record(text: str, reply: str, *, ts_user: float) -> None:
            self.recorded.append((text, reply))

        m._record_external_exchange = _record  # pyright: ignore[reportAttributeAccessIssue]

        async def _classify(text: str) -> str:
            # The model path starts here; stop the turn so the test can see it got this far.
            self.classified.append(text)
            raise _ReachedTheModel

        m._classify_intent = _classify  # pyright: ignore[reportAttributeAccessIssue]
        self.actor = m

    def ask(self, text: str) -> str | None:
        """Main's reply, or None when the turn went on to the model."""
        try:
            return asyncio.run(self.actor.process_user_input(text))
        except _ReachedTheModel:
            return None

    def ask_streaming(self, text: str) -> list[Any]:
        """What the streaming turn yields, up to the model if it gets there."""

        async def collect() -> list[Any]:
            chunks: AsyncIterator[Any] = self.actor.process_user_input_stream(text)
            return [chunk async for chunk in chunks]

        try:
            return asyncio.run(collect())
        except _ReachedTheModel:
            return []


class _ReachedTheModel(Exception):
    """Raised where a turn would call the model."""


class TestReadingAPlainSpawnRequest:
    """The name, out of a request that is only a verb and a name."""

    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("spawn reachy mini", "reachy mini"),
            ("Spawn Reachy Mini.", "Reachy Mini"),
            ("spawn reachy-mini", "reachy-mini"),
            ("start the anomaly detector", "anomaly detector"),
            ("launch a manual agent", "manual agent"),
            ("run reachy_mini", "reachy_mini"),
            ("please start the reachy mini agent please!", "reachy mini agent"),
        ],
    )
    def test_the_name_is_what_follows_the_verb(self, text: str, name: str) -> None:
        assert spawn_request_name(text) == name

    @pytest.mark.parametrize(
        "text",
        [
            "spawn an agent that watches the reachy mini battery",
            "spawn a new agent for a reachy mini robot",
            "what does reachy mini do?",
            "tell catalog to spawn reachy mini",
            "spawn",
        ],
    )
    def test_a_request_with_more_in_it_names_nothing(self, text: str) -> None:
        assert spawn_request_name(text) is None


class TestStartingARecipeByName:
    """A request naming a recipe goes to the catalog, as ``@catalog spawn`` does."""

    @pytest.mark.parametrize(
        "text",
        [
            "spawn reachy mini",
            "spawn Reachy Mini",
            "start the reachy mini agent",
            "spawn reachy_mini",
            "launch reachy-mini",
        ],
    )
    def test_any_spelling_of_the_name_reaches_the_catalog(self, text: str) -> None:
        main = _Main()

        answer = main.ask(text)

        assert answer == "catalog: spawning"
        assert main.delegation.mentions == ["@catalog spawn reachy-mini"]
        assert not main.classified

    def test_a_recipe_named_with_agent_is_matched(self) -> None:
        main = _Main()

        main.ask("spawn the manual agent")

        assert main.delegation.mentions == ["@catalog spawn manual-agent"]

    def test_the_exchange_is_kept_in_history(self) -> None:
        main = _Main()

        main.ask("spawn reachy mini")

        assert main.recorded == [("spawn reachy mini", "catalog: spawning")]

    def test_the_streaming_turn_starts_it_too(self) -> None:
        main = _Main()

        chunks = main.ask_streaming("spawn reachy mini")

        assert chunks[0] == "catalog: spawning"
        assert chunks[-1]["done"] is True
        assert main.delegation.mentions == ["@catalog spawn reachy-mini"]
        assert not main.classified

    @pytest.mark.parametrize(
        "text",
        [
            "spawn an agent that watches the reachy mini battery",
            "start monitoring my cpu",
            "spawn reachy mini on node pi",
            "spawn weather",
        ],
    )
    def test_anything_else_goes_to_the_model(self, text: str) -> None:
        main = _Main()

        assert main.ask(text) is None

        assert main.classified == [text]
        assert not main.delegation.mentions


class TestTheModelIsShownTheRecipes:
    """The system prompt lists what the catalog can start, and how."""

    def prompt(self, running: tuple[str, ...] = ("catalog",)) -> str:
        main = MainActor(llm_provider=None)
        main._registry = _Registry(running)  # pyright: ignore[reportAttributeAccessIssue]
        main._agent_manifests = dict(RECIPES)
        main._rebuild_system_prompt()
        return main.system_prompt

    def test_each_recipe_is_listed_with_what_it_does(self) -> None:
        prompt = self.prompt()

        assert "== CATALOGUE RECIPES" in prompt
        assert "reachy-mini — Drives a Reachy Mini robot" in prompt
        assert "anomaly-detector — Flags unusual sensor readings" in prompt

    def test_the_prompt_says_how_to_have_the_catalog_start_one(self) -> None:
        prompt = self.prompt()

        assert '{"agent": "catalog", "payload": {"action": "spawn"' in prompt
        assert "'reachy mini'" in prompt

    def test_a_running_recipe_is_not_listed_as_startable(self) -> None:
        prompt = self.prompt(running=("catalog", "reachy-mini"))

        recipes = prompt.split("== CATALOGUE RECIPES", 1)[1]
        assert "reachy-mini —" not in recipes.split("\n\n==", 1)[0]

    def test_a_manifest_the_catalog_did_not_write_is_not_listed(self) -> None:
        main = MainActor(llm_provider=None)
        main._agent_manifests = {"weather": cast(AgentManifest, {"description": "A forecast"})}
        main._rebuild_system_prompt()

        assert "== CATALOGUE RECIPES" not in main.system_prompt

    def test_a_beta_recipe_says_so(self) -> None:
        main = MainActor(llm_provider=None)
        main._agent_manifests = {"probe": recipe("Tries things", experimental=True)}
        main._rebuild_system_prompt()

        assert "probe (beta) — Tries things" in main.system_prompt
