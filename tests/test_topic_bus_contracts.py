"""Topic matching and the contract registry.

Topic patterns decide which agent receives which message, so a matching bug is
silent: the wrong agent is wired, or none is, and nothing raises.
"""

from __future__ import annotations

import pytest

from wactorz.core.topic_bus import TopicContract, TopicRegistry, _topic_matches


class TestTopicMatching:
    """MQTT wildcards: `+` is one level, `#` is the rest."""

    @pytest.mark.parametrize(
        "pattern",
        ["sensors/kitchen/temp", "sensors/+/temp", "sensors/#", "#", "sensors/kitchen/#"],
    )
    def test_patterns_that_match(self, pattern: str) -> None:
        assert _topic_matches(pattern, "sensors/kitchen/temp")

    @pytest.mark.parametrize(
        "pattern",
        [
            "sensors/kitchen",
            "sensors/+",
            "sensors/bath/temp",
            "sensors/kitchen/temp/extra",
            "other/#",
        ],
    )
    def test_patterns_that_do_not(self, pattern: str) -> None:
        assert not _topic_matches(pattern, "sensors/kitchen/temp")

    def test_plus_matches_exactly_one_level_not_zero(self) -> None:
        assert not _topic_matches("sensors/+/temp", "sensors/temp")

    def test_plus_matches_one_level_not_several(self) -> None:
        assert not _topic_matches("sensors/+", "sensors/kitchen/temp")

    def test_hash_matches_the_remainder_including_none_of_it(self) -> None:
        assert _topic_matches("sensors/#", "sensors")

    def test_a_plus_in_the_last_position_still_needs_a_level(self) -> None:
        assert _topic_matches("sensors/+", "sensors/kitchen")
        assert not _topic_matches("sensors/+", "sensors")

    def test_an_exact_string_matches_before_any_splitting(self) -> None:
        assert _topic_matches("a/b/c", "a/b/c")

    def test_matching_is_directional(self) -> None:
        """The pattern holds the wildcards; a wildcard in the topic is literal."""
        assert _topic_matches("sensors/#", "sensors/kitchen")
        assert not _topic_matches("sensors/kitchen", "sensors/#")


class TestContractNormalisation:
    """Contracts arrive from model-authored code, so the fields are defensive."""

    def test_a_bare_string_becomes_a_one_topic_list(self) -> None:
        c = TopicContract(
            name="a",
            publishes="sensors/temp",  # type: ignore[arg-type]
            subscribes="cmd/all",  # type: ignore[arg-type]
        )
        assert c.publishes == ["sensors/temp"]
        assert c.subscribes == ["cmd/all"]

    @pytest.mark.parametrize("bogus", ["publishes", "subscribes", "topic", "schema", "name"])
    def test_kwarg_names_passed_as_topics_are_dropped(self, bogus: str) -> None:
        """Model output sometimes passes the argument name as its value."""
        c = TopicContract(name="a", publishes=[bogus, "sensors/temp"])
        assert c.publishes == ["sensors/temp"]

    def test_a_contract_can_end_up_with_no_topics(self) -> None:
        c = TopicContract(name="a", publishes=["publishes"], subscribes=["subscribes"])
        assert not c.publishes
        assert not c.subscribes


class TestContractQueries:
    def test_matches_topic_uses_the_subscribe_patterns(self) -> None:
        c = TopicContract(name="a", subscribes=["sensors/+/temp"])
        assert c.matches_topic("sensors/kitchen/temp")
        assert not c.matches_topic("sensors/kitchen/humidity")

    def test_produces_topic_matches_in_both_directions(self) -> None:
        """A query may be broader or narrower than the declaration.

        An agent publishing `sensors/kitchen/temp` answers a query for
        `sensors/#`, and one declaring `sensors/#` answers a query for a leaf.
        """
        leaf = TopicContract(name="leaf", publishes=["sensors/kitchen/temp"])
        assert leaf.produces_topic("sensors/#")

        wide = TopicContract(name="wide", publishes=["sensors/#"])
        assert wide.produces_topic("sensors/kitchen/temp")

    def test_update_observed_records_field_types_from_a_real_payload(self) -> None:
        c = TopicContract(name="a", publishes=["sensors/temp"])
        c.update_observed("sensors/temp", {"value": 21.5, "unit": "C", "_seq": 3})

        sample = c.observed_samples["sensors/temp"]
        assert sample["fields"] == {"value": "float", "unit": "str"}
        assert sample["example"] == {"value": 21.5, "unit": "C"}

    def test_update_observed_ignores_a_non_dict_payload(self) -> None:
        c = TopicContract(name="a")
        c.update_observed("sensors/temp", "not a dict")  # type: ignore[arg-type]
        assert not c.observed_samples


class TestRegistry:
    @pytest.fixture(name="registry")
    def registry_fixture(self) -> TopicRegistry:
        reg = TopicRegistry()
        reg.register(TopicContract(name="thermo", publishes=["sensors/kitchen/temp"]))
        reg.register(TopicContract(name="alarm", subscribes=["sensors/+/temp"]))
        reg.register(TopicContract(name="idle", publishes=["other/thing"]))
        return reg

    def test_register_then_get(self, registry: TopicRegistry) -> None:
        assert registry.get("thermo") is not None
        assert registry.get("absent") is None

    def test_registering_the_same_name_replaces(self, registry: TopicRegistry) -> None:
        registry.register(TopicContract(name="thermo", publishes=["new/topic"]))
        assert len(registry.all_contracts()) == 3
        contract = registry.get("thermo")
        assert contract is not None
        assert contract.publishes == ["new/topic"]

    def test_unregister_removes(self, registry: TopicRegistry) -> None:
        registry.unregister("thermo")
        assert registry.get("thermo") is None

    def test_unregistering_something_absent_is_harmless(self, registry: TopicRegistry) -> None:
        registry.unregister("never-existed")

    def test_producers_and_consumers_of_a_topic(self, registry: TopicRegistry) -> None:
        producers = [c.name for c in registry.producers_of("sensors/kitchen/temp")]
        consumers = [c.name for c in registry.consumers_of("sensors/kitchen/temp")]
        assert producers == ["thermo"]
        assert consumers == ["alarm"]

    def test_find_by_capability_searches_topics_and_the_name(self, registry: TopicRegistry) -> None:
        assert {c.name for c in registry.find_by_capability("kitchen")} == {"thermo"}
        assert {c.name for c in registry.find_by_capability("THERMO")} == {"thermo"}
        assert {c.name for c in registry.find_by_capability("sensors")} == {"thermo", "alarm"}

    def test_capability_search_is_literal_and_does_not_expand_wildcards(
        self, registry: TopicRegistry
    ) -> None:
        """`alarm` subscribes to `sensors/+/temp`, which would match
        `sensors/kitchen/temp` — but the pattern string holds no "kitchen", so a
        keyword search for it does not find the agent. Discovery by keyword and
        routing by pattern answer different questions.
        """
        assert registry.consumers_of("sensors/kitchen/temp")[0].name == "alarm"
        assert "alarm" not in {c.name for c in registry.find_by_capability("kitchen")}

    def test_wiring_opportunities_pair_a_publisher_with_a_subscriber(
        self, registry: TopicRegistry
    ) -> None:
        pairs = registry.find_wiring_opportunities()
        assert [(p.name, c.name, t) for p, c, t in pairs] == [
            ("thermo", "alarm", "sensors/kitchen/temp")
        ]

    def test_an_agent_is_never_wired_to_itself(self) -> None:
        reg = TopicRegistry()
        reg.register(TopicContract(name="loop", publishes=["x/y"], subscribes=["x/y"]))
        assert not reg.find_wiring_opportunities()

    def test_prune_stale_drops_contracts_whose_agent_is_gone(self, registry: TopicRegistry) -> None:
        pruned = registry.prune_stale({"thermo", "alarm"})
        assert pruned == ["idle"]
        assert registry.get("idle") is None
        assert registry.get("thermo") is not None

    def test_prune_stale_with_everything_live_removes_nothing(
        self, registry: TopicRegistry
    ) -> None:
        assert not registry.prune_stale({"thermo", "alarm", "idle"})

    def test_summary_counts_topics_and_pairs(self, registry: TopicRegistry) -> None:
        summary = registry.summary()
        assert summary["total_agents"] == 3
        assert summary["total_published"] == 2
        assert summary["total_subscribed"] == 1
        assert summary["wiring_pairs"] == 1

    def test_planner_context_names_the_agents(self, registry: TopicRegistry) -> None:
        context = registry.to_planner_context()
        assert "thermo" in context
        assert "sensors/kitchen/temp" in context
