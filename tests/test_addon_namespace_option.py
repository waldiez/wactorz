# SPDX-License-Identifier: Apache-2.0
"""The namespace is reachable from the Home Assistant add-on, and changes nothing unset.

Configured by three files that cannot see each other: `wactorz/config.py` reads
`WACTORZ_NAMESPACE`, the add-on's `config.yaml` decides what the Supervisor
offers and its default, and `run.sh` turns the option into the variable, with a
fallback of its own for an options file written before the option existed. Both
the default and the fallback are empty, which is no namespace: an existing
install keeps every topic it has.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ["wactorz", "wactorz-ultra"]
OPTION = "namespace"
VARIABLE = "WACTORZ_NAMESPACE"


def _config(addon: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / "ha-addon" / addon / "config.yaml").read_text(encoding="utf-8"))


def _export(addon: str) -> tuple[str, str] | None:
    """(option read, fallback) for the variable, as `run.sh` states it, read out of the script."""
    source = (ROOT / "ha-addon" / addon / "run.sh").read_text(encoding="utf-8")
    match = re.search(
        rf"^{VARIABLE}=\$\(get_config_safe '(\w+)' '([^']*)'\)\nexport {VARIABLE}$",
        source,
        flags=re.MULTILINE,
    )
    return (match.group(1), match.group(2)) if match else None


@pytest.mark.parametrize("addon", ADDONS)
class TestTheNamespaceIsWired:
    def test_it_is_offered_empty(self, addon: str) -> None:
        assert _config(addon)["options"][OPTION] == ""

    def test_the_schema_takes_any_text_or_none(self, addon: str) -> None:
        # What may be in it is checked at start (`namespace_error`), with the reason.
        assert _config(addon)["schema"][OPTION] == "str?"

    def test_run_sh_exports_it_with_an_empty_fallback(self, addon: str) -> None:
        assert _export(addon) == (OPTION, "")

    def test_the_docs_list_it(self, addon: str) -> None:
        docs = (ROOT / "ha-addon" / addon / "DOCS.md").read_text(encoding="utf-8")
        assert f"| `{OPTION}` | *(empty)* |" in docs
