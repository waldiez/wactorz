"""Agent programs are Python held in a string, so the linters do not read them.

Each catalogue agent keeps its program in `AGENT_CODE` and execs it at spawn.
Ruff and the type checker see a string literal, so a syntax error there survives
every check and surfaces as an agent that will not start.

Parsing is not coverage. It is the floor.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

AGENTS = pathlib.Path(__file__).resolve().parent.parent / "wactorz" / "catalogue_agents"

#: The name the loader asks for: `getattr(mod, "AGENT_CODE", None)`. Matching on
#: it rather than on "a long string" keeps prompts out, and keeps this in step
#: with what actually gets run.
NAME = "AGENT_CODE"


def agent_sources() -> list[tuple[str, str, str]]:
    """(module, name, source) for every embedded agent program."""
    found = []
    for path in sorted(AGENTS.glob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            if not any(isinstance(t, ast.Name) and t.id == NAME for t in node.targets):
                continue
            found.append((path.name, NAME, value.value))
    return found


def commented_out() -> list[str]:
    """Modules whose program has been commented out and not put back.

    An easy thing to leave behind after reading the code with the linter, and
    the recipe is dead until it goes back: the loader finds no attribute.
    """
    out = []
    for path in sorted(AGENTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        live = any(line.startswith(f"{NAME} = ") for line in text.splitlines())
        commented = any(
            line.lstrip("# ").startswith(f"{NAME} = ") and line.lstrip().startswith("#")
            for line in text.splitlines()
        )
        if commented and not live:
            out.append(path.name)
    return out


SOURCES = agent_sources()


def _enclosing(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def function_local_stdlib_imports(source: str) -> list[tuple[str, int]]:
    """Stdlib imports inside a function that no `try` makes optional.

    A third-party import belongs in a function when it lets the agent start
    without that package. A stdlib import cannot fail, so it has no such excuse.
    """
    tree = ast.parse(source)
    parents = _enclosing(tree)
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        modules = (
            [alias.name.split(".")[0] for alias in node.names]
            if isinstance(node, ast.Import)
            else [(node.module or "").split(".")[0]]
        )
        in_function = in_try = False
        cursor = parents.get(node)
        while cursor is not None:
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                in_function = True
            if isinstance(cursor, ast.Try):
                in_try = True
            cursor = parents.get(cursor)
        if in_function and not in_try:
            out.extend(
                (module, node.lineno) for module in modules if module in sys.stdlib_module_names
            )
    return out


def test_there_is_agent_source_to_check() -> None:
    """Without this, renaming the convention would make every check below vacuous."""
    assert SOURCES, f"no {NAME} found under {AGENTS}"


def test_no_program_is_left_commented_out() -> None:
    """A commented-out program is a recipe the loader cannot find."""
    assert not commented_out(), f"{NAME} is commented out in: {', '.join(commented_out())}"


@pytest.mark.parametrize(
    ("module", "name", "source"), SOURCES, ids=[f"{m}:{n}" for m, n, _ in SOURCES]
)
def test_every_agent_program_parses(module: str, name: str, source: str) -> None:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        pytest.fail(
            f"{module}:{name} is not valid Python at line {exc.lineno} of the "
            f"embedded source: {exc.msg}"
        )


#: How many of these the programs still carry. A ceiling, not a target: it fails
#: on growth, never on progress, so it can be lowered whenever a program is
#: cleaned up rather than having to move in the same commit.
FUNCTION_LOCAL_STDLIB_CEILING = 62


def test_stdlib_imports_do_not_spread() -> None:
    """A stdlib import cannot fail, so a function is never the place for it."""
    offenders = [
        f"{module}:{imported}@{lineno}"
        for module, _name, source in SOURCES
        for imported, lineno in function_local_stdlib_imports(source)
    ]

    assert len(offenders) <= FUNCTION_LOCAL_STDLIB_CEILING, (
        f"{len(offenders) - FUNCTION_LOCAL_STDLIB_CEILING} new stdlib import(s) inside "
        f"a function: {', '.join(offenders[FUNCTION_LOCAL_STDLIB_CEILING:])}"
    )
