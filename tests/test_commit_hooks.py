"""The commit hooks check a commit with the project's own environment.

They are `language: system` hooks, so a command in one is looked up on PATH. A
bare `python3` or `ruff` there is the project's only while its virtualenv is
activated; otherwise the commit is checked by the system interpreter, which
lacks the dependencies, and by whatever version of a tool the system has. So
each Python hook asks the Makefile which interpreter to run, as every `make`
target does.

The test hook also leaves out test files git has not been told about. The hook
sets unstaged changes aside before it runs and leaves untracked files where
they are, so such a file would run against code that is no longer there, and
fail a commit it is no part of.
"""

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

#: The hooks that run Python or a tool installed with it.
PYTHON_HOOKS = ["ruff-format", "ruff-check", "basedpyright", "python-tests"]

#: How a hook names the Makefile's interpreter.
INTERPRETER = '"$(make -s python-path)"'


def _hooks() -> dict[str, dict]:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    (local,) = [repo for repo in config["repos"] if repo["repo"] == "local"]
    return {hook["id"]: hook for hook in local["hooks"]}


def _recipe(target: str) -> str:
    """The lines of the Makefile recipe for ``target``."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    found = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)+)", makefile, re.MULTILINE)
    assert found is not None, f"the Makefile has no `{target}` target"
    return found.group(1)


@pytest.mark.parametrize("hook", PYTHON_HOOKS)
def test_a_python_hook_runs_the_interpreter_the_makefile_picks(hook: str) -> None:
    entry = _hooks()[hook]["entry"]
    command = shlex.split(entry)

    assert command[0] in {"bash", "make"}, (
        f"`{hook}` starts `{command[0]}` from PATH, which is the project's only while its "
        "virtualenv is activated"
    )
    if command[0] == "bash":
        assert INTERPRETER in entry


def test_no_hook_has_come_to_run_python_some_other_way() -> None:
    # A hook added later, under a name the list above does not know.
    for name, hook in _hooks().items():
        first = shlex.split(hook["entry"])[0]
        assert first not in {"python", "python3", "ruff", "pytest", "basedpyright"}, name


def test_the_makefile_says_which_interpreter_that_is() -> None:
    assert _recipe("python-path").strip() == "@echo $(PYTHON)"


def test_the_hooks_that_take_file_names_pass_them_on() -> None:
    # `bash -c '<script>' --`: what follows the script becomes its arguments,
    # and the first of them is taken as the script's name.
    for hook in ("ruff-format", "ruff-check"):
        command = shlex.split(_hooks()[hook]["entry"])

        assert command[:2] == ["bash", "-c"]
        assert command[2].endswith('"$@"')
        assert command[3:] == ["--"]


def test_the_test_hook_leaves_out_files_git_does_not_know() -> None:
    assert shlex.split(_hooks()["python-tests"]["entry"]) == ["make", "test-py-tracked"]
    recipe = _recipe("test-py-tracked")

    assert "$(PYTHON) -m pytest tests" in recipe
    assert "git ls-files --others --exclude-standard" in recipe
    assert "--ignore=" in recipe


def test_the_type_check_hook_leaves_them_out_too() -> None:
    # It reads every file under the configured folders unless given the files,
    # and an untracked one is then checked against code the hook has set aside.
    assert shlex.split(_hooks()["basedpyright"]["entry"]) == ["make", "typecheck-tracked"]
    recipe = _recipe("typecheck-tracked")

    assert "$(PYTHON) -m basedpyright $$(git ls-files -- " in recipe
    assert "--others" not in recipe
    for folder in ("wactorz", "tests", "scripts"):
        assert f"'{folder}/*.py'" in recipe


def test_the_hooks_are_installed_with_the_projects_own_prek() -> None:
    assert _recipe("precommit-install").strip() == "$(PYTHON) -m prek install"
