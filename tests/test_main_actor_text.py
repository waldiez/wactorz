"""Text main normalises before it uses it.

Two pure functions with no relationship to each other beyond that: one
canonicalises an agent name so a user's spelling matches a spawned agent's, the
other removes the system-state block main injects into its own prompt so that
block is not later mistaken for something the user said.

Both are pinned as they behave, including where that is surprising, so a
deliberate change breaks a test that explains what it was guarding.
"""

from typing import Any

from wactorz.agents.main_actor import _normalize_agent_name, _strip_live_context


class TestNormalizeAgentName:
    """Fuzzy-matching canonicalisation: case, separators, and the -agent suffix."""

    def test_the_documented_collapses(self) -> None:
        assert _normalize_agent_name("Smart Energy Agent") == "smart-energy"
        assert _normalize_agent_name("smart_energy_agent") == "smart-energy"
        assert _normalize_agent_name("smart-energy") == "smart-energy"
        assert _normalize_agent_name("  Spaced  Out  ") == "spaced-out"
        assert _normalize_agent_name("--dashes--") == "dashes"
        assert _normalize_agent_name("agent") == "agent"

    def test_an_empty_name_stays_empty(self) -> None:
        assert not _normalize_agent_name("")

    def test_a_none_name_is_tolerated_rather_than_raising(self) -> None:
        """The `name or ""` guard means callers may pass None.

        Pinned because the signature says `str` and the guard says otherwise —
        a caller relies on one of them and it is not the annotation.
        """
        assert not _normalize_agent_name(None)  # type: ignore[arg-type]

    def test_the_bare_suffix_guard_is_unreachable(self) -> None:
        """`norm != "-agent"` can never be false, and this pins why.

        `norm.strip("-")` runs *before* the suffix check, so by the time the
        guard is evaluated a leading dash is already gone: "-agent" has become
        "agent", which does not end with "-agent" and is returned whole. The
        guard reads as protection against emptying the name and protects
        nothing, because the case it names cannot reach it.

        Pinned as dead-but-harmless. Removing the guard is safe and changes no
        output; this test is what says so.
        """
        assert _normalize_agent_name("-agent") == "agent"
        assert _normalize_agent_name("--agent--") == "agent"


class TestStripLiveContext:
    """Removing the injected system-state block before facts are extracted."""

    def test_the_block_and_its_trailing_blank_lines_go(self) -> None:
        message = "[CURRENT SYSTEM STATE]\nagents: a, b\n[END SYSTEM STATE]\n\n  Turn on the light"
        assert _strip_live_context(message) == "Turn on the light"

    def test_a_message_without_the_block_is_untouched(self) -> None:
        assert _strip_live_context("Turn on the light") == "Turn on the light"

    def test_an_opening_marker_with_no_close_is_left_alone(self) -> None:
        """Returning the whole message is the safe direction.

        Guessing where an unterminated block ends would silently eat real user
        text; leaving it means fact extraction sees noise, which is recoverable.
        """
        message = "[CURRENT SYSTEM STATE]\nagents: a, b"
        assert _strip_live_context(message) == message

    def test_a_non_string_is_passed_straight_back(self) -> None:
        """The isinstance guard means callers may hand this anything."""
        value: Any = {"not": "a string"}
        assert _strip_live_context(value) is value
