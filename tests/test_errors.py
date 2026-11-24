# SPDX-License-Identifier: Apache-2.0
"""`wactorz.errors` and the library guide's *What to catch* say the same thing.

The module names every exception a library user can be handed; the guide's
table says which call raises each and which built-in it is a kind of. Both are
checked here, so an exception added to one and not the other, a base changed
without the guide, or a call that can raise one the guide does not give it,
fails.

Which call raises what is found by reading the code: each call's body, and the
bodies of the wactorz functions and methods it calls by a name the reader can
follow (``self.x``, a module global, ``Cls.method``), as far as they go. A call
through a local variable is not followed, so what is found is the least a call
raises; an ``except`` is not read, so an error caught and raised again as
another shows up as both, and `_NOT_REACHED` says which those are.
"""

import ast
import asyncio
import builtins
import inspect
import re
import textwrap
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import wactorz
from wactorz import errors
from wactorz.agents.rule_agent import RuleConfig
from wactorz.core.actor import Actor
from wactorz.core.integrations import ag2

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "library.md"

#: What each name in the table's *Raised by* column is.
_CALLS: dict[str, Callable[..., Any]] = {
    "wactorz.run": wactorz.run,
    "wactorz.serve": wactorz.serve,
    "wactorz.ask": wactorz.ask,
    "wactorz.agent": wactorz.agent,
    "wactorz.pipeline": wactorz.pipeline,
    "RuleConfig.from_dict": RuleConfig.from_dict,
    "Actor.ask": Actor.ask,
    "Actor.subscribe": Actor.subscribe,
    "notify_user": Actor.notify_user,
    "ask_operator": Actor.ask_operator,
    "wait_for_answer": Actor.wait_for_answer,
    "label": Actor.label,
    "artifact_path": Actor.artifact_path,
    "load_model": Actor.load_model,
    "recall_bytes": Actor.recall_bytes,
    "wactorz.core.integrations.ag2.model_config": ag2.model_config,
}

#: Raises the code holds that cannot reach the caller, by call, with why.
_NOT_REACHED: dict[str, dict[str, str]] = {
    "ask_operator": {
        "NotCallableError": "its answer listener is its own method",
        "NotFoundError": "it waits on the question it has just written",
    },
    "wait_for_answer": {"NotCallableError": "its answer listener is its own method"},
    "wactorz.pipeline": {
        "OptionsError": "a rule's question re-raises it as RuleError",
        "UnknownChannelError": "a rule's question re-raises it as RuleError",
    },
    "RuleConfig.from_dict": {
        "OptionsError": "a rule's question re-raises it as RuleError",
        "UnknownChannelError": "a rule's question re-raises it as RuleError",
    },
}


def _section(text: str, heading: str) -> str:
    """The body of the ``## heading`` section of ``text``."""
    start = text.index(f"## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else None]


def _table() -> list[list[str]]:
    """The cells of each row of *What to catch*'s table, header and rule left out."""
    rows = []
    for line in _section(GUIDE.read_text(encoding="utf-8"), "What to catch").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or cells[1] in ("Exception", "") or set(cells[1]) <= {"-"}:
            continue
        rows.append(cells)
    return rows


def _names(cell: str) -> list[str]:
    return re.findall(r"`([\w.]+)`", cell)


def _rows() -> list[tuple[list[str], list[str]]]:
    """Each table row: the exceptions it names, and their kinds."""
    return [(_names(cells[1]), _names(cells[2])) for cells in _table()]


def _documented() -> dict[str, set[str]]:
    """The exceptions the table gives each call in its *Raised by* column."""
    found: dict[str, set[str]] = {}
    for cells in _table():
        for call in _names(cells[0]):
            found.setdefault(call, set()).update(_names(cells[1]))
    return found


_EXPORTED = {getattr(errors, name): name for name in errors.__all__}


def _resolve(expr: ast.expr, scope: dict[str, Any], owner: type | None) -> Any:
    """What ``expr``, a dotted name, is in ``scope``; ``self`` is ``owner``. None if unknown."""
    parts = ast.unparse(expr).split(".")
    if parts[0] == "self":
        found: Any = owner
    else:
        found = scope.get(parts[0])
    for part in parts[1:]:
        if found is None:
            return None
        found = (
            inspect.getattr_static(found, part, None)
            if inspect.isclass(found)
            else getattr(found, part, None)
        )
    if isinstance(found, (staticmethod, classmethod)):
        found = found.__func__
    return found


def _raised_here(node: ast.Raise, scope: dict[str, Any], owner: type | None) -> str | None:
    """The exported exception ``node`` raises, as ``Cls(...)`` or ``Cls.reason(...)``."""
    if node.exc is None:
        return None
    target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
    found = _resolve(target, scope, owner)
    if not inspect.isclass(found) and isinstance(target, ast.Attribute):
        found = _resolve(target.value, scope, owner)
    return _EXPORTED.get(found) if inspect.isclass(found) else None


def _raised(fn: Any, owner: type | None, seen: set[Any]) -> set[str]:
    """The exported exceptions ``fn`` raises, itself or through what it calls."""
    fn = getattr(fn, "__func__", fn)
    if fn in seen or not isinstance(fn, types.FunctionType):
        return set()
    if not fn.__module__.startswith("wactorz"):
        return set()
    seen.add(fn)
    found: set[str] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if isinstance(node, ast.Raise):
            name = _raised_here(node, fn.__globals__, owner)
            if name is not None:
                found.add(name)
        elif isinstance(node, ast.Call):
            on_self = ast.unparse(node.func).startswith("self.")
            callee = _resolve(node.func, fn.__globals__, owner)
            found |= _raised(callee, owner if on_self else None, seen)
    return found


def _kind(name: str) -> type[BaseException]:
    """The class a *A kind of* cell names: a built-in, `asyncio`'s, or one of ours."""
    if name.startswith("asyncio."):
        found = getattr(asyncio, name.removeprefix("asyncio."))
    else:
        found = getattr(builtins, name, None) or getattr(errors, name)
    assert isinstance(found, type) and issubclass(found, BaseException), name
    return found


def test_every_name_the_module_exports_is_an_exception() -> None:
    for name in errors.__all__:
        found = getattr(errors, name)
        assert isinstance(found, type) and issubclass(found, Exception), name


def test_the_guide_names_every_exception_the_module_exports() -> None:
    raised = {exc for excs, _ in _rows() for exc in excs}
    kinds = {kind for _, names in _rows() for kind in names}

    # Each row's exception is exported, and each export is in a row, raised or
    # as the kind a raised one belongs to (`ArtifactError`, `ModelError`'s base).
    assert raised <= set(errors.__all__)
    assert set(errors.__all__) <= raised | kinds


@pytest.mark.parametrize(
    ("excs", "kinds"), _rows(), ids=lambda value: "-".join(value) if isinstance(value, list) else ""
)
def test_each_exception_is_the_kind_the_guide_says(excs: list[str], kinds: list[str]) -> None:
    # A cell names one kind for all its exceptions, or one kind each.
    assert len(kinds) in (1, len(excs)), (excs, kinds)
    for exc, kind in zip(excs, kinds * len(excs) if len(kinds) == 1 else kinds, strict=True):
        assert issubclass(getattr(errors, exc), _kind(kind)), f"{exc} is not a {kind}"


def test_every_call_the_table_names_is_one_this_test_knows() -> None:
    assert set(_documented()) == set(_CALLS)


@pytest.mark.parametrize("call", sorted(_CALLS))
def test_the_table_gives_each_call_every_exception_it_can_raise(call: str) -> None:
    fn = _CALLS[call]
    owner = Actor if getattr(fn, "__qualname__", "").startswith("Actor.") else None
    not_reached = _NOT_REACHED.get(call, {})

    missing = _raised(fn, owner, set()) - _documented()[call] - set(not_reached)

    assert not missing, f"{call} can raise {sorted(missing)}, which the guide does not say"


def test_the_package_root_keeps_the_names_it_had() -> None:
    assert wactorz.StartupError is errors.StartupError
