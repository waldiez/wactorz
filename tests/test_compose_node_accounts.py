"""The compose stacks give every deployed node a broker account of its own.

The compose broker is configured here, so it knows the accounts Wactorz derives:
`mqtt-certs` writes them and the access list for the broker, and the app hands
them to a node at `/deploy`. Both read `WACTORZ_NODE_ACCOUNTS`, so both default
it on, and `.env` can still turn it off. Outside compose the default stays off,
because a broker Wactorz does not configure would know no such accounts.
"""

import os
import re
from pathlib import Path

import pytest
import yaml

from wactorz import config

ROOT = Path(__file__).resolve().parents[1]

#: Each compose file, and the services that act on the setting.
SERVICES = {
    "compose.yaml": ("mqtt-certs", "wactorz-python"),
    "compose.dev.yaml": ("mqtt-certs", "wactorz"),
}
VARIABLE = "WACTORZ_NODE_ACCOUNTS"


def _environment(name: str, service: str) -> dict[str, str]:
    compose = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
    return compose["services"][service].get("environment") or {}


def _resolve(value: str, env: dict[str, str]) -> str:
    """Compose's `${NAME:-default}`: the default when NAME is unset or empty."""

    def substitute(match: re.Match[str]) -> str:
        return env.get(match.group(1)) or match.group(2)

    return re.sub(r"\$\{(\w+):-([^}]*)\}", substitute, value)


@pytest.mark.parametrize(
    ("name", "service"), [(name, s) for name, services in SERVICES.items() for s in services]
)
class TestTheComposeDefault:
    def test_it_is_on_when_env_says_nothing(self, name: str, service: str) -> None:
        value = _environment(name, service)[VARIABLE]

        assert _resolve(value, {}) == "1"
        assert _resolve(value, {VARIABLE: ""}) == "1"

    def test_env_can_still_turn_it_off(self, name: str, service: str) -> None:
        value = _environment(name, service)[VARIABLE]

        assert _resolve(value, {VARIABLE: "0"}) == "0"


def test_both_services_in_a_file_agree() -> None:
    # One writing accounts the other does not hand out -- or the reverse -- leaves
    # a node authenticating as someone the broker does not know.
    for name, services in SERVICES.items():
        values = {_environment(name, service)[VARIABLE] for service in services}
        assert len(values) == 1, name


def test_outside_compose_it_stays_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VARIABLE, raising=False)

    assert config._env_truthy(VARIABLE) is False
    assert os.getenv(VARIABLE) is None
