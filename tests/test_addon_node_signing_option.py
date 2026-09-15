"""The node signing mode is reachable from the Home Assistant add-on.

Configured by three files that cannot see each other: `wactorz/config.py` reads
`WACTORZ_NODE_SIGNING`, the add-on's `config.yaml` decides what the Supervisor
offers and its default, and `run.sh` turns the option into the variable, with a
fallback of its own for an options file written before the option existed.

The default warns. A node that refuses what it cannot check stops working the
moment anything is misconfigured, and an option only appears after the update
that brings it, so a refusing default would act on an install before its owner
could see the setting.
"""

import re
from pathlib import Path

import pytest
import yaml

from wactorz import config

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ["wactorz", "wactorz-ultra"]
OPTION = "node_signing"
VARIABLE = "WACTORZ_NODE_SIGNING"


def _config(addon: str) -> dict:
    return yaml.safe_load((ROOT / "ha-addon" / addon / "config.yaml").read_text(encoding="utf-8"))


def _export(addon: str) -> tuple[str, str] | None:
    """(option read, fallback) for the variable, as `run.sh` states it.

    Read out of the script rather than by running it: `run.sh` needs bashio and
    the Supervisor, and the thing that drifts is the text itself.
    """
    source = (ROOT / "ha-addon" / addon / "run.sh").read_text(encoding="utf-8")
    match = re.search(
        rf"^{VARIABLE}=\$\(get_config_safe '(\w+)' '([^']*)'\)\nexport {VARIABLE}=\"\$\{{{VARIABLE}\}}\"$",
        source,
        flags=re.MULTILINE,
    )
    return (match.group(1), match.group(2)) if match else None


@pytest.mark.parametrize("addon", ADDONS)
class TestTheModeIsWired:
    def test_it_is_offered_and_warns_by_default(self, addon: str) -> None:
        assert _config(addon)["options"][OPTION] == "warn"

    def test_the_schema_offers_exactly_the_modes_wactorz_accepts(self, addon: str) -> None:
        # A mode missing here cannot be chosen; one Wactorz does not know falls back
        # to warn with a warning, which would surprise someone who picked it.
        schema = _config(addon)["schema"][OPTION]
        match = re.fullmatch(r"list\(([\w|]+)\)\??", schema)
        assert match, schema
        assert tuple(match.group(1).split("|")) == config.NODE_SIGNING_MODES

    def test_run_sh_exports_it_with_the_same_default(self, addon: str) -> None:
        assert _export(addon) == (OPTION, _config(addon)["options"][OPTION])


def test_the_exported_variable_is_the_one_wactorz_reads() -> None:
    source = (ROOT / "wactorz" / "config.py").read_text(encoding="utf-8")
    assert f'os.getenv("{VARIABLE}"' in source


def test_both_addons_agree() -> None:
    first, second = (_config(addon) for addon in ADDONS)
    assert first["options"][OPTION] == second["options"][OPTION]
    assert first["schema"][OPTION] == second["schema"][OPTION]
    assert _export(ADDONS[0]) == _export(ADDONS[1])
