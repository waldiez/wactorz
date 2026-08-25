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

#: A string this size assigned to a module-level name is agent source, not prose.
MIN_LINES = 20

#: A ceiling, not a target. Existing ones are left to a change of their own.
#: Lower it as they go; raising it means an import that belongs at the top.
FUNCTION_LOCAL_STDLIB_CEILING = 62


def agent_sources() -> list[tuple[str, str, str]]:
    """(module, name, source) for every embedded agent program."""
    found = []
    for path in sorted(AGENTS.glob("*.py")):
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.Assign):
                continue
            value = node.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            if len(value.value.splitlines()) < MIN_LINES:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.append((path.name, target.id, value.value))
    return found


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
    assert SOURCES, f"no embedded agent source found under {AGENTS}"


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


def test_stdlib_imports_do_not_drift_further_into_functions() -> None:
    """Hold the line on imports that belong at the top of their agent."""
    offenders = [
        (module, name, module_name, lineno)
        for module, name, source in SOURCES
        for module_name, lineno in function_local_stdlib_imports(source)
    ]

    if len(offenders) > FUNCTION_LOCAL_STDLIB_CEILING:
        added = len(offenders) - FUNCTION_LOCAL_STDLIB_CEILING
        sample = ", ".join(f"{m}:{mod}@{ln}" for m, _n, mod, ln in offenders[-added:])
        pytest.fail(f"{added} new stdlib import(s) inside a function: {sample}")

    assert len(offenders) == FUNCTION_LOCAL_STDLIB_CEILING, (
        f"lower FUNCTION_LOCAL_STDLIB_CEILING to {len(offenders)}"
    )
