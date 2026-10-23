"""The prompts a configured Home Assistant installation sends, pinned word for word.

Main and the planner are being reshaped so that a system without Home Assistant
gets prompts that never mention it. The promise that makes such a change safe
to review is that an installation *with* Home Assistant keeps the prompts it has
today, byte for byte. These tests are that promise: each prompt, as the model
receives it on the Home Assistant profile, is compared with a copy kept under
``tests/parity_fixtures/prompts/``.

A fixture that no longer matches is not a test to fix; it is a prompt change to
review. When the change is intended, regenerate the fixtures with

    WACTORZ_UPDATE_PROMPT_FIXTURES=1 pytest tests/test_prompts_are_pinned.py

and read the fixture diff in the pull request as you would read the prompt.

The fixtures are JSON, one line of the prompt per array element, rather than
plain text: an editor that trims trailing whitespace or adds a final newline
would otherwise alter the pinned text without anyone asking it to, and a line
per element keeps the diff readable.
"""

from __future__ import annotations

import difflib
import json
import os
import pathlib
from collections.abc import Callable

import pytest

from wactorz.agents.main.actor import MainActor
from wactorz.agents.prompts.main_actor_prompts import (
    FACTS_EXTRACT_PROMPT,
    INTENT_CLASSIFIER_PROMPT,
)
from wactorz.agents.prompts.planner_prompts import (
    DECOMPOSE_PROMPT,
    PIPELINE_DESIGN_PROMPT,
    RULE_CONFLICT_PROMPT,
)

FIXTURE_DIR = pathlib.Path(__file__).parent / "parity_fixtures" / "prompts"

#: Set in the environment to rewrite the fixtures from the current prompts.
UPDATE_ENV_VAR = "WACTORZ_UPDATE_PROMPT_FIXTURES"

#: How many lines of a diff a failure shows before pointing at the fixture.
DIFF_LINES_SHOWN = 40


def main_system_prompt(tmp_path: pathlib.Path) -> str:
    """The system prompt main sends on a turn, with no agents running and no facts.

    Built by the same method main calls before every model turn, so the override
    block and the running-agents block are part of what is pinned, not only the
    constant they wrap. No registry and no persisted facts: the live parts are
    empty, which leaves exactly the text that is the same on every installation.
    """
    main = MainActor(llm_provider=None, persistence_dir=str(tmp_path))
    main._rebuild_system_prompt()
    return main.system_prompt


#: Fixture name -> how to produce the prompt the model receives today.
PROMPTS: dict[str, Callable[[pathlib.Path], str]] = {
    "main_system_prompt": main_system_prompt,
    "intent_classifier": lambda _tmp: INTENT_CLASSIFIER_PROMPT,
    "facts_extract": lambda _tmp: FACTS_EXTRACT_PROMPT,
    "pipeline_design": lambda _tmp: PIPELINE_DESIGN_PROMPT,
    "decompose": lambda _tmp: DECOMPOSE_PROMPT,
    "rule_conflict": lambda _tmp: RULE_CONFLICT_PROMPT,
}


def fixture_path(name: str) -> pathlib.Path:
    return FIXTURE_DIR / f"{name}.json"


def read_fixture(name: str) -> str:
    """The pinned prompt, or an empty string when nothing has been pinned yet."""
    path = fixture_path(name)
    if not path.exists():
        return ""
    data = json.loads(path.read_text(encoding="utf-8"))
    return "\n".join(data["lines"])


def write_fixture(name: str, text: str) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"prompt": name, "lines": text.split("\n")}
    fixture_path(name).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _diff(pinned: str, current: str, name: str) -> str:
    lines = list(
        difflib.unified_diff(
            pinned.split("\n"),
            current.split("\n"),
            fromfile=f"pinned/{name}",
            tofile=f"current/{name}",
            lineterm="",
        )
    )
    shown = "\n".join(lines[:DIFF_LINES_SHOWN])
    if len(lines) > DIFF_LINES_SHOWN:
        shown += f"\n... ({len(lines) - DIFF_LINES_SHOWN} more diff lines)"
    return shown


@pytest.mark.parametrize("name", sorted(PROMPTS))
def test_the_prompt_is_what_was_pinned(name: str, tmp_path: pathlib.Path) -> None:
    current = PROMPTS[name](tmp_path)
    if os.environ.get(UPDATE_ENV_VAR):
        write_fixture(name, current)
    pinned = read_fixture(name)

    assert pinned, (
        f"No fixture for {name!r}. Pin the current prompt with "
        f"{UPDATE_ENV_VAR}=1 pytest {pathlib.Path(__file__).name}"
    )
    assert current == pinned, (
        f"The {name} prompt differs from its pinned copy. If the change is intended, "
        f"regenerate with {UPDATE_ENV_VAR}=1 and review the fixture diff.\n\n"
        + _diff(pinned, current, name)
    )


def test_every_fixture_file_belongs_to_a_prompt() -> None:
    """A fixture nobody compares against is a promise nobody keeps."""
    present = {p.stem for p in FIXTURE_DIR.glob("*.json")}

    assert present == set(PROMPTS), present ^ set(PROMPTS)


def test_the_fixture_round_trips_exactly() -> None:
    """Splitting on newlines and joining again must be lossless, including a
    trailing newline and blank lines, or the comparison would be weaker than it
    looks.
    """
    for text in ("a\nb", "a\n\nb\n", "\n", "", "trailing space \n"):
        assert "\n".join(text.split("\n")) == text
