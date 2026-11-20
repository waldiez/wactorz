# SPDX-License-Identifier: Apache-2.0
"""The package's entry points: what a library user calls, and what `python -m` runs.

``wactorz.run`` and the rest are the real functions, so an editor shows their
keyword arguments and a type checker refuses a misspelled one.

A module run with ``python -m`` holds a command and nothing the rest of the
package imports. Loaded already when it starts, runpy warns, and the command
fails under ``-W error``.
"""

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

import wactorz
import wactorz.app
import wactorz.pipelines


def test_run_serve_and_system_are_the_app_s_own() -> None:
    assert wactorz.run is wactorz.app.run
    assert wactorz.serve is wactorz.app.serve
    assert wactorz.system is wactorz.app.system


def test_pipeline_is_the_pipelines_module_s_own() -> None:
    assert wactorz.pipeline is wactorz.pipelines.pipeline


def test_their_keyword_arguments_are_named() -> None:
    for function in (wactorz.run, wactorz.serve, wactorz.pipeline):
        kinds = {parameter.kind for parameter in inspect.signature(function).parameters.values()}
        assert inspect.Parameter.VAR_KEYWORD not in kinds, function.__name__


#: The package's source, where the modules run with `python -m` are found.
_PACKAGE = Path(wactorz.__file__).parent


def _runnable_modules() -> list[str]:
    """Every module with a ``__main__`` block, as `python -m` names it."""
    found = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path.name == "__main__.py" or 'if __name__ == "__main__"' not in path.read_text(
            encoding="utf-8"
        ):
            continue
        parts = path.relative_to(_PACKAGE.parent).with_suffix("").parts
        found.append(".".join(parts))
    return found


#: Run in a fresh interpreter: what runpy checks before it runs a module, which
#: is whether importing its parent package already loaded it.
_LOADED_BEFORE_IT_RUNS = (
    "import importlib, sys; "
    "name = sys.argv[1]; importlib.import_module(name.rpartition('.')[0]); "
    "print(name in sys.modules)"
)


@pytest.mark.parametrize("module", _runnable_modules())
def test_a_module_run_with_python_m_is_not_loaded_before_it_runs(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _LOADED_BEFORE_IT_RUNS, module],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert result.stdout.strip() == "False"


def test_the_modules_run_with_python_m_are_found() -> None:
    assert {"wactorz.cli", "wactorz.broker_certificates"} <= set(_runnable_modules())
