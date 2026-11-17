# SPDX-License-Identifier: Apache-2.0
"""An agent's avatar, from where it is said to where the dashboard reads it.

The avatar is a small dictionary: a class names a ready-made character with
`AVATAR_KIND`, a spawn config may ask for one of its own, and every heartbeat
carries the result, which the web layer copies out for the dashboard. The
dictionary's shape is checked here; what its words mean is the dashboard's.
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from aiohttp import web

from wactorz.agents.catalog_agent import _build_catalog
from wactorz.agents.function_agent import FunctionAgent
from wactorz.agents.rule_agent import RuleAgent
from wactorz.core.actor import Actor
from wactorz.core.avatars import AvatarError, checked_avatar, spawn_avatar
from wactorz.node.agent import NodeAgent
from wactorz.node.runner import NodeRunner
from wactorz.web import events, runtime
from wactorz.web.api_actors import _actor_payload, actors_handler
from wactorz.web.wiring import dressed


class Plain(Actor):
    """An actor whose class names no character."""

    async def handle_message(self, message: Any) -> None:
        """Take nothing."""


class Sailor(Plain):
    """An actor whose class names a character."""

    AVATAR_KIND = "main"


@pytest.fixture(name="clean_state", autouse=True)
def clean_state_fixture() -> Any:
    """Each case starts with no agents recorded."""
    before = runtime.state.get("agents")
    runtime.state["agents"] = {}
    yield
    runtime.state["agents"] = before if before is not None else {}


class TestTheCheck:
    def test_a_kind_and_parts_are_taken_as_given(self) -> None:
        asked = {"kind": "weather-agent", "accent": "#5b8def", "ant": 2, "top": ""}

        assert checked_avatar(asked) == asked

    def test_the_result_is_a_copy(self) -> None:
        asked = {"kind": "main"}

        assert checked_avatar(asked) is not asked

    @pytest.mark.parametrize(
        ("asked", "words"),
        [
            ("main", "not a str"),
            (["main"], "not a list"),
            ({f"part_{letter}": 1 for letter in "abcdefghijklm"}, "at most"),
            ({"Kind": "main"}, "part name"),
            ({7: "main"}, "part name"),
            ({"kind": "a name with spaces"}, "'kind'"),
            ({"kind": "x" * 33}, "'kind'"),
            ({"kind": "<svg>"}, "'kind'"),
            ({"ant": 10}, "'ant'"),
            ({"ant": -1}, "'ant'"),
            ({"ant": True}, "'ant'"),
            ({"accent": 1.5}, "'accent'"),
            ({"parts": {"neck": "tie"}}, "'parts'"),
        ],
    )
    def test_anything_else_is_refused_with_its_reason(self, asked: object, words: str) -> None:
        with pytest.raises(AvatarError, match=words):
            checked_avatar(asked)


class TestWhatASpawnAsksFor:
    def test_a_config_without_one_asks_for_none(self) -> None:
        assert spawn_avatar({"name": "a"}, "a") is None

    def test_a_good_one_is_what_the_agent_wears(self) -> None:
        assert spawn_avatar({"avatar": {"kind": "flic"}}, "a") == {"kind": "flic"}

    def test_a_bad_one_is_left_out_and_said(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="wactorz.core.avatars"):
            assert spawn_avatar({"avatar": "flic"}, "button") is None

        assert "'button'" in caplog.text
        assert "not a str" in caplog.text


class TestWhatAnActorWears:
    def test_a_class_without_a_kind_wears_nothing(self, tmp_path: Path) -> None:
        assert Plain(name="plain", persistence_dir=str(tmp_path)).avatar == {}

    def test_a_class_with_a_kind_wears_it(self, tmp_path: Path) -> None:
        assert Sailor(name="sailor", persistence_dir=str(tmp_path)).avatar == {"kind": "main"}

    def test_what_the_spawn_gave_comes_before_the_class(self, tmp_path: Path) -> None:
        actor = Sailor(name="sailor", persistence_dir=str(tmp_path), avatar={"kind": "flic"})

        assert actor.avatar == {"kind": "flic"}

    def test_two_actors_of_a_class_do_not_share_one_dictionary(self, tmp_path: Path) -> None:
        first = Sailor(name="first", persistence_dir=str(tmp_path))
        second = Sailor(name="second", persistence_dir=str(tmp_path))
        first.avatar["accent"] = "#000000"

        assert second.avatar == {"kind": "main"}

    def test_the_heartbeat_carries_it(self, tmp_path: Path) -> None:
        actor = Sailor(name="sailor", persistence_dir=str(tmp_path))

        assert actor._build_heartbeat()["avatar"] == {"kind": "main"}

    def test_the_shipped_agents_name_their_characters(self) -> None:
        assert RuleAgent.AVATAR_KIND == "rule"
        assert FunctionAgent.AVATAR_KIND == "function"

    def test_an_agent_on_a_node_wears_what_its_spawn_gave(self, tmp_path: Path) -> None:
        runner = NodeRunner("localhost", 1883, "rpi", state_dir=str(tmp_path))
        agent = NodeAgent({"name": "edge", "code": "", "avatar": {"kind": "flic"}}, runner)

        assert agent._build_heartbeat()["avatar"] == {"kind": "flic"}


class TestTheCatalogue:
    def test_every_dynamic_recipe_is_drawn_as_itself(self) -> None:
        dynamic = {
            key: recipe
            for key, recipe in _build_catalog().items()
            if recipe.get("type") == "dynamic"
        }

        assert dynamic
        assert {key: recipe["avatar"] for key, recipe in dynamic.items()} == {
            key: {"kind": key} for key in dynamic
        }

    def test_what_a_recipe_asks_for_passes_the_check(self) -> None:
        for recipe in _build_catalog().values():
            if "avatar" in recipe:
                assert checked_avatar(recipe["avatar"]) == recipe["avatar"]


class TestTheTripToTheDashboard:
    @staticmethod
    def _beat(**fields: Any) -> dict[str, Any]:
        events.record_heartbeat("a1", {"name": "agent", "state": "running", **fields})
        return _actor_payload(runtime.state["agents"]["a1"] | {"agent_id": "a1"})

    def test_the_avatar_reaches_the_card(self) -> None:
        assert self._beat(avatar={"kind": "flic"})["avatar"] == {"kind": "flic"}

    def test_an_agent_that_sends_none_has_an_empty_one(self) -> None:
        assert self._beat()["avatar"] == {}

    def test_an_agent_started_again_without_one_loses_the_old(self) -> None:
        self._beat(avatar={"kind": "flic"})

        assert self._beat(avatar={})["avatar"] == {}

    def test_something_that_is_not_a_dictionary_is_not_kept(self) -> None:
        self._beat(avatar={"kind": "flic"})

        assert self._beat(avatar="main")["avatar"] == {"kind": "flic"}


class TestTheWiring:
    def test_each_agent_of_the_graph_is_given_the_avatar_it_reported(self) -> None:
        events.record_heartbeat(
            "a1", {"name": "known", "state": "running", "avatar": {"kind": "flic"}}
        )
        graph = dressed(
            {"agents": [{"id": "a1", "name": "known"}, {"id": "a2", "name": "unheard"}]}
        )

        assert [agent["avatar"] for agent in graph["agents"]] == [{"kind": "flic"}, {}]

    def test_an_agent_of_this_process_says_so_itself(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Before any heartbeat of its has been recorded.
        sailor = Sailor(name="sailor", persistence_dir=str(tmp_path))
        monkeypatch.setattr(
            runtime,
            "registry",
            SimpleNamespace(get=lambda actor_id: sailor if actor_id == sailor.actor_id else None),
        )

        graph = dressed({"agents": [{"id": sailor.actor_id, "name": "sailor"}]})

        assert graph["agents"][0]["avatar"] == {"kind": "main"}

    def test_a_graph_with_no_agents_is_left_as_it_is(self) -> None:
        assert dressed({"topics": []}) == {"topics": []}


class TestTheListOfActors:
    async def test_an_agent_of_this_process_is_listed_with_what_it_wears(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # From the actor itself: no heartbeat has been recorded for either.
        actors = [
            SimpleNamespace(
                actor_id="a1", name="worn", protected=False, metrics=None, avatar={"kind": "flic"}
            ),
            SimpleNamespace(actor_id="a2", name="bare", protected=False, metrics=None, avatar={}),
        ]
        monkeypatch.setattr(
            runtime, "registry", SimpleNamespace(all_actors=lambda: actors, get=lambda _id: None)
        )

        response = await actors_handler(cast(web.Request, SimpleNamespace(app=SimpleNamespace())))

        assert isinstance(response.body, bytes)
        assert [a["avatar"] for a in json.loads(response.body)] == [{"kind": "flic"}, {}]
