"""Every broker Wactorz configures can rewrite its own password file.

`mosquitto_passwd -c` creates the file and refuses when one is already there,
and each of these brokers writes its file again: the compose broker on every
start and whenever the node accounts change, the add-on's embedded broker on
every restart. A refusal there leaves the broker without its accounts, so each
`-c` has the file removed just before it.

The same files carry the settings that shape the access list, from an add-on's
options to the variables `wactorz/config.py` reads, and name the broker release
compose runs, which is the one the broker tests run against.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ADDONS = ["wactorz", "wactorz-ultra"]

#: Everything that runs `mosquitto_passwd -c`.
SCRIPTS = [
    "compose.yaml",
    "compose.dev.yaml",
    *(f"ha-addon/{addon}/run.sh" for addon in ADDONS),
]

#: An add-on option, and the variable it becomes.
SETTINGS = {
    "node_topics": "WACTORZ_NODE_TOPICS",
    "broker_accounts": "WACTORZ_BROKER_ACCOUNTS",
}

_CREATE = re.compile(r"^\s*mosquitto_passwd\s+-b\s+-c\s+(\S+)")


def _lines(path: str) -> list[str]:
    return (ROOT / path).read_text(encoding="utf-8").splitlines()


def _created(path: str) -> list[tuple[str, str]]:
    """(file created, the command on the line before) for each `-c` in `path`."""
    lines = _lines(path)
    found = []
    for index, line in enumerate(lines):
        match = _CREATE.match(line)
        if match:
            found.append((match.group(1), lines[index - 1].strip()))
    return found


def _addon(addon: str) -> dict:
    return yaml.safe_load((ROOT / "ha-addon" / addon / "config.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", SCRIPTS)
class TestThePasswordFileIsReplaced:
    def test_the_file_is_created_here(self, path: str) -> None:
        # Guards the tests below: a rewritten command that no longer matches
        # would otherwise pass them by finding nothing to check.
        assert _created(path), f"{path} no longer runs `mosquitto_passwd -b -c <file>`"

    def test_it_is_removed_before_it_is_created(self, path: str) -> None:
        for target, before in _created(path):
            assert before == f"rm -f {target}", (
                f"{path}: `mosquitto_passwd -c {target}` fails when the file exists; "
                f"the line before it must be `rm -f {target}`"
            )


@pytest.mark.parametrize("addon", ADDONS)
@pytest.mark.parametrize(("option", "variable"), SETTINGS.items())
class TestTheAccessListSettingsReachAnAddon:
    def test_the_option_is_offered_empty_and_optional(
        self, addon: str, option: str, variable: str
    ) -> None:
        # Empty is the access list an install had before the option existed.
        config = _addon(addon)
        assert config["options"][option] == ""
        assert config["schema"][option] == "str?"

    def test_run_sh_exports_it(self, addon: str, option: str, variable: str) -> None:
        source = "\n".join(_lines(f"ha-addon/{addon}/run.sh"))
        assert f"{variable}=$(get_config_safe '{option}' '')\nexport {variable}\n" in source


@pytest.mark.parametrize("variable", SETTINGS.values())
def test_the_exported_variable_is_one_wactorz_reads(variable: str) -> None:
    source = (ROOT / "wactorz" / "config.py").read_text(encoding="utf-8")
    assert f'os.getenv("{variable}"' in source


_BROKER_IMAGE = r"(eclipse-mosquitto:\S+@sha256:[0-9a-f]{64})"


def _compose_broker(name: str) -> str:
    compose = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    return compose["services"]["mosquitto"]["image"]


def _tested_broker() -> str:
    text = (ROOT / ".github" / "tools" / "Dockerfile").read_text(encoding="utf-8")
    found = re.search(rf"^FROM {_BROKER_IMAGE} AS mosquitto$", text, re.MULTILINE)
    assert found is not None, "the tools Dockerfile pins mosquitto by tag and digest"
    return found.group(1)


@pytest.mark.parametrize("name", ["compose.yaml", "compose.dev.yaml"])
def test_compose_runs_the_broker_the_broker_tests_run(name: str) -> None:
    # Dependabot moves the two in separate pull requests. What each rule of the
    # access list allows is checked against the tools image, so a compose broker
    # on another release would be running a list nothing had tried on it.
    assert _compose_broker(name) == _tested_broker(), (
        f"Dependabot moved one of them: give {name}'s mosquitto and the `AS mosquitto` "
        "line in .github/tools/Dockerfile the same image, then run `make test-broker`"
    )
