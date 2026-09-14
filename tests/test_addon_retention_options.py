"""Every retention window is reachable from the Home Assistant add-on.

A window is configured by three files that have no way of knowing about each
other: `wactorz/config.py` reads `WACTORZ_RETENTION_<STORE>_DAYS`, the add-on's
`config.yaml` decides which options the Supervisor offers and their defaults, and
`run.sh` turns those options into environment variables, with a fallback of its
own for an options file that lacks the key.

The add-on keeps chat for ever by default. An option only appears after the
update that brings it, so a default that deletes would act on an existing
install before its owner could see the setting, let alone change it.
"""

import os
import re

import pytest
import yaml

ADDONS = ["wactorz", "wactorz-ultra"]

#: Add-on option → the environment variable Wactorz reads for it.
OPTIONS = {
    "retention_chat_days": "WACTORZ_RETENTION_CHAT_DAYS",
    "retention_timeseries_days": "WACTORZ_RETENTION_TIMESERIES_DAYS",
    "retention_outbox_days": "WACTORZ_RETENTION_OUTBOX_DAYS",
}


def _repo_path(relative: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, relative)


def _read(relative: str) -> str:
    with open(_repo_path(relative), encoding="utf-8") as handle:
        return handle.read()


def _config(addon: str) -> dict:
    return yaml.safe_load(_read(f"ha-addon/{addon}/config.yaml"))


def _exports(addon: str) -> dict[str, tuple[str, str]]:
    """Variable → (option it is read from, fallback), as `run.sh` states them.

    Read out of the script rather than by running it: `run.sh` needs bashio and
    the Supervisor, and the thing that drifts is the text itself.
    """
    source = _read(f"ha-addon/{addon}/run.sh")
    found = re.findall(
        r"^(WACTORZ_RETENTION_\w+)=\$\(get_config_safe '(\w+)' '([^']*)'\)\n"
        r"export \1=\"\$\{\1\}\"$",
        source,
        flags=re.MULTILINE,
    )
    return {variable: (option, fallback) for variable, option, fallback in found}


@pytest.mark.parametrize("addon", ADDONS)
class TestEveryWindowIsWired:
    def test_every_option_is_offered_with_a_default(self, addon: str) -> None:
        # A key missing from `options:` has no default, and one missing from
        # `schema:` is refused by the Supervisor when entered.
        config = _config(addon)
        for option in OPTIONS:
            assert option in config["options"], option
            assert config["schema"].get(option) == "int", option

    def test_every_option_is_exported_as_the_variable_wactorz_reads(self, addon: str) -> None:
        exports = _exports(addon)
        assert {variable: option for variable, (option, _) in exports.items()} == {
            variable: option for option, variable in OPTIONS.items()
        }

    def test_the_fallback_in_run_sh_matches_the_default(self, addon: str) -> None:
        # Otherwise an options file written before the option existed gets a
        # different window from a fresh install.
        defaults = _config(addon)["options"]
        for variable, (option, fallback) in _exports(addon).items():
            assert fallback == str(defaults[option]), variable

    def test_chat_is_kept_for_ever_by_default(self, addon: str) -> None:
        assert _config(addon)["options"]["retention_chat_days"] == 0
        assert _exports(addon)["WACTORZ_RETENTION_CHAT_DAYS"][1] == "0"


def test_every_exported_variable_is_one_wactorz_reads() -> None:
    # A renamed setting in config.py would leave the add-on exporting a name
    # nothing reads, and the library default would apply instead.
    source = _read("wactorz/config.py")
    for variable in OPTIONS.values():
        assert f'_env_int("{variable}"' in source, variable


def test_both_addons_agree() -> None:
    first, second = (_exports(addon) for addon in ADDONS)
    assert first == second
    defaults = [
        {option: _config(addon)["options"][option] for option in OPTIONS} for addon in ADDONS
    ]
    assert defaults[0] == defaults[1]
