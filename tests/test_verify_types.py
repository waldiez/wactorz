# SPDX-License-Identifier: Apache-2.0
"""What `make verifytypes` says about a completeness report, and when it fails.

The run itself builds an environment and installs the package, which the suite
does not do; what it makes of basedpyright's report is checked here on reports
written by hand.
"""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _load_script() -> ModuleType:
    """Import `scripts/verify_types.py`, which is not an installed module."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_types.py"
    spec = importlib.util.spec_from_file_location("verify_types", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


verify_types = _load_script()


def _symbol(name: str, known: bool, why: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "isExported": True,
        "isTypeKnown": known,
        "diagnostics": [{"message": why}] if why else [],
    }


REPORT: dict[str, Any] = {
    "completenessScore": 0.75,
    "exportedSymbolCounts": {"withKnownType": 3, "withAmbiguousType": 0, "withUnknownType": 1},
    "symbols": [
        _symbol("wactorz.core.actor.Actor", True),
        _symbol(
            "wactorz.core.actor.Actor.state", False, 'Type is missing type annotation "x"\nmore'
        ),
        _symbol("wactorz.agents.x.f", False, 'Type of parameter "a" is partially unknown'),
        _symbol("wactorz.agents.x.g", False, 'Type of parameter "b" is partially unknown'),
        {**_symbol("wactorz.web.private", False, "Hidden"), "isExported": False},
    ],
}


class TestTheSummary:
    def test_it_gives_the_score_and_the_counts(self) -> None:
        first = verify_types._summary(REPORT, top=5).splitlines()[0]

        assert first == "Type completeness: 75.00%  (known 3, ambiguous 0, unknown 1)"

    def test_it_counts_the_exported_unknowns_by_package(self) -> None:
        summary = verify_types._summary(REPORT, top=5)

        assert "      2  wactorz.agents" in summary
        assert "      1  wactorz.core" in summary
        # Not exported: not part of what users see.
        assert "wactorz.web" not in summary

    def test_it_groups_the_reasons_by_their_words_before_the_names(self) -> None:
        summary = verify_types._summary(REPORT, top=5)

        assert "      2  Type of parameter" in summary
        assert "      1  Type is missing type annotation" in summary

    def test_top_limits_each_list(self) -> None:
        summary = verify_types._summary(REPORT, top=1)

        assert "wactorz.core" not in summary


class TestTheFloor:
    def test_a_score_below_it_says_both(self) -> None:
        with pytest.raises(SystemExit, match=r"75\.00% is below the floor of 80\.00%"):
            raise verify_types.BelowFloorError(75.0, 80.0)

    def test_a_package_found_outside_the_scratch_environment_is_refused(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit, match="not in the scratch environment"):
            raise verify_types.NotTheInstallError(tmp_path / "wactorz")
