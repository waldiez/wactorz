"""`pip install 'wactorz[reachy]'` installs exactly what the Reachy recipe would.

The extra exists so the packages resolve together with Wactorz's own before it
starts, and the first spawn then has nothing to install and needs no restart.
That only holds while the two lists agree: a package in the recipe but not the
extra is installed at spawn after all, and can bring the restart back with it.
"""

from pathlib import Path

import pytest

from wactorz.agents.catalog_agent import _build_catalog

tomllib = pytest.importorskip("tomllib")  # standard library from Python 3.11

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _extras() -> dict[str, list[str]]:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)["project"]["optional-dependencies"]


def test_the_extra_lists_exactly_what_the_recipe_installs() -> None:
    assert _extras()["reachy"] == _build_catalog()["reachy-mini"]["install"]


def test_the_extra_stays_out_of_all() -> None:
    # `all` builds the Docker image and the Home Assistant add-ons, which cannot
    # compile the robot SDK's Linux dependencies.
    assert "wactorz[reachy]" not in _extras()["all"]
