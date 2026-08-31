"""An agent's name becomes a directory, so it has to be refused as one.

Replacing separators is not enough: `..` contains none, so it survived the
substitution and walked up a level — an agent called `..` kept its state in the
state directory's own parent, and `POST /api/reset {"agent": ".."}` reached the
same path.

Worth more than its size because of what lives there: these are pickle files
that get loaded on the next start, and loading a pickle someone else placed is
code execution rather than bad data.

The check that holds is containment after `resolve()`, not the list of nasty
strings above it. A blocklist has to have predicted every way out; resolving and
then asking "is this still inside" does not.
"""

from pathlib import Path

import pytest

from wactorz.core.paths import agent_state_dir

ESCAPES = ["..", "../..", "../etc", "..//..", "  ..  ", "../"]


class TestNamesThatMustBeRefused:
    @pytest.mark.parametrize("name", ESCAPES)
    def test_a_name_that_climbs_out_is_refused(self, tmp_path: Path, name: str) -> None:
        with pytest.raises(ValueError):
            agent_state_dir(tmp_path, name)

    @pytest.mark.parametrize("name", ["", "   ", "."])
    def test_a_name_that_is_no_name_is_refused(self, tmp_path: Path, name: str) -> None:
        # Each of these resolves to the state directory itself, so the agent
        # would keep its state where every other agent's directory lives.
        with pytest.raises(ValueError):
            agent_state_dir(tmp_path, name)

    @pytest.mark.parametrize("name", ["/", "\\", "./..", "a/../.."])
    def test_a_name_made_only_of_separators_lands_inside(self, tmp_path: Path, name: str) -> None:
        # Not refused, and it does not need to be: the separators become
        # underscores, so what is left is an odd but harmless directory name
        # under the base. Asserted because "looks dangerous" and "escapes" are
        # different questions, and only the second one matters here.
        assert tmp_path.resolve() in agent_state_dir(tmp_path, name).parents

    def test_the_refusal_names_the_agent(self, tmp_path: Path) -> None:
        # The operator has to be able to find which agent is misconfigured.
        with pytest.raises(ValueError, match=r"\.\."):
            agent_state_dir(tmp_path, "..")


class TestNamesThatAreFine:
    @pytest.mark.parametrize(
        "name", ["main", "weather-agent", "doc_to_pptx", "agent.v2", "Rpi-Kitchen", "a"]
    )
    def test_an_ordinary_name_is_kept_as_it_is(self, tmp_path: Path, name: str) -> None:
        # The directory an install already has must not move: renaming it would
        # silently orphan that agent's state on upgrade.
        assert agent_state_dir(tmp_path, name) == (tmp_path / name).resolve()

    def test_separators_still_become_underscores(self, tmp_path: Path) -> None:
        # Long-standing behaviour, and the reason `../etc` is refused rather
        # than quietly turning into `.._etc`: the dots are what escape, not the
        # slashes, so the substitution alone was never the guard.
        assert agent_state_dir(tmp_path, "a/b").name == "a_b"

    def test_a_name_beginning_with_two_dots_is_refused_even_so(self, tmp_path: Path) -> None:
        # `..hidden` cannot escape — it is one directory name — but it is
        # refused anyway. Deliberately conservative: nothing legitimate is
        # called that, and the narrower rule would have to be right about every
        # platform's path parsing rather than only this one's.
        with pytest.raises(ValueError):
            agent_state_dir(tmp_path, "..hidden")

    def test_it_does_not_create_anything(self, tmp_path: Path) -> None:
        # Callers differ on when the directory should exist; one that only wants
        # to know where state would go must not leave a directory behind.
        agent_state_dir(tmp_path, "weather-agent")

        assert not (tmp_path / "weather-agent").exists()


class TestEveryPlaceThatBuildsOne:
    """The three stores that turn a name into a path all go through the guard.

    Asserted by behaviour rather than by reading the source, because the failure
    being prevented is one of them keeping its own copy of the rule and drifting.
    """

    def test_the_actor_refuses_to_start_under_such_a_name(self, tmp_path: Path) -> None:
        from wactorz.core.actor import Actor

        class _Actor(Actor):
            async def handle_message(self, message: object) -> None:
                return None

        with pytest.raises(ValueError):
            _Actor(name="..", persistence_dir=str(tmp_path))

    def test_the_pickle_store_refuses_too(self, tmp_path: Path) -> None:
        from wactorz.core.persistence import PickleStore

        with pytest.raises(ValueError):
            PickleStore(str(tmp_path)).save("..", {"secret": 1})

    def test_a_good_name_still_works_everywhere(self, tmp_path: Path) -> None:
        from wactorz.core.persistence import PickleStore

        store = PickleStore(str(tmp_path))

        assert store.save("weather-agent", {"seen": 1})
        assert store.load("weather-agent") == {"seen": 1}
