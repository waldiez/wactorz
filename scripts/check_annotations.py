"""Report functions missing annotations, or using a bare container type.

Neither ruff nor basedpyright flags a missing annotation, so a clean run of
those proves nothing here. This is the check that does.

    python3 scripts/check_annotations.py wactorz/agents/dynamic/*.py

Exits non-zero when anything is found, so it can gate.
"""

import ast
import sys
from pathlib import Path

#: Containers that say nothing on their own; parameterise or use dict[str, Any].
BARE = frozenset({"dict", "list", "set", "tuple", "frozenset"})

#: Parameters that never carry an annotation.
IMPLICIT = frozenset({"self", "cls"})

AnyFunc = ast.FunctionDef | ast.AsyncFunctionDef


def bare_names(annotation: ast.expr) -> list[str]:
    """Container names in an annotation that are not themselves subscripted.

    `dict` inside `list[dict[str, Any]]` is already parameterised, so only a
    name that no `Subscript` claims as its value counts as bare.
    """
    subscripted = {
        id(node.value) for node in ast.walk(annotation) if isinstance(node, ast.Subscript)
    }
    return [
        node.id
        for node in ast.walk(annotation)
        if isinstance(node, ast.Name) and node.id in BARE and id(node) not in subscripted
    ]


def parameters(fn: AnyFunc) -> list[ast.arg]:
    """Every parameter of `fn`, including *args and **kwargs."""
    args = fn.args
    extra = [a for a in (args.vararg, args.kwarg) if a is not None]
    return [*args.posonlyargs, *args.args, *args.kwonlyargs, *extra]


def check_function(fn: AnyFunc, path: Path) -> list[str]:
    """Everything wrong with one function's annotations."""
    problems: list[str] = []
    where = f"{path}:{fn.lineno} {fn.name}"

    if fn.returns is None:
        problems.append(f"  [no-return]   {where}()")
    else:
        problems += [f"  [bare-return] {where}() -> ...{b}..." for b in bare_names(fn.returns)]

    for arg in parameters(fn):
        if arg.arg in IMPLICIT:
            continue
        if arg.annotation is None:
            problems.append(f"  [no-param]    {where}({arg.arg})")
        else:
            problems += [
                f"  [bare-param]  {where}({arg.arg}: ...{b}...)" for b in bare_names(arg.annotation)
            ]
    return problems


def check_file(path: Path) -> list[str]:
    """Everything wrong in one file, in source order."""
    tree = ast.parse(path.read_text())
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            problems += check_function(node, path)
    return problems


def main(argv: list[str]) -> int:
    problems: list[str] = []
    for arg in argv:
        problems += check_file(Path(arg))
    for line in problems:
        print(line)
    print(f"\n  TOTAL: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
