"""The programs the catalogue hands to a DynamicAgent compile, and keep to the rules.

A catalogue program is source sent in a spawn config's `code` and exec'd at
spawn. Most are modules of `wactorz.catalogue_agents`, which the linters read
like any other; one still keeps its program in an `AGENT_CODE` string, which
they see only as a literal. These checks run on what the catalogue actually
sends, so they hold for both shapes.
"""

import ast
import pathlib
import sys

import pytest

from wactorz.agents.catalog_agent import _build_catalog

AGENTS = pathlib.Path(__file__).resolve().parent.parent / "wactorz" / "catalogue_agents"

#: The name `_load_embedded_recipe` asks for.
NAME = "AGENT_CODE"


def catalogue_programs() -> list[tuple[str, str]]:
    """(recipe, source) for every catalogue recipe that runs as a program."""
    return sorted(
        (name, recipe["code"]) for name, recipe in _build_catalog().items() if recipe.get("code")
    )


def commented_out() -> list[str]:
    """Modules whose `AGENT_CODE` has been commented out and not put back.

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


PROGRAMS = catalogue_programs()


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


def test_there_are_programs_to_check() -> None:
    """Without this, a loader that found nothing would make every check below vacuous."""
    assert PROGRAMS, "the catalogue built no recipe that carries code"


def test_no_program_is_left_commented_out() -> None:
    """A commented-out program is a recipe the loader cannot find."""
    assert not commented_out(), f"{NAME} is commented out in: {', '.join(commented_out())}"


@pytest.mark.parametrize(("recipe", "source"), PROGRAMS, ids=[name for name, _ in PROGRAMS])
def test_every_program_compiles(recipe: str, source: str) -> None:
    try:
        compile(source, recipe, "exec")
    except SyntaxError as exc:
        pytest.fail(f"{recipe} is not valid Python at line {exc.lineno}: {exc.msg}")


def test_no_program_imports_the_stdlib_inside_a_function() -> None:
    """A stdlib import cannot fail, so a function is never the place for it."""
    offenders = [
        f"{recipe}:{imported}@{lineno}"
        for recipe, source in PROGRAMS
        for imported, lineno in function_local_stdlib_imports(source)
    ]

    assert offenders == []
