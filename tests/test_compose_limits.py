"""The compose stacks cap what each container can take, and publish to this host only.

Without a ceiling, a leak or a runaway agent ends with the host out of memory or
process ids, and everything else on it goes down too. With one, the container
is restarted. The ceilings are far above what a service uses, and the app's are
settings, since what an agent loads is not something a compose file can know.

A published port with no address in front is published on every interface,
past any firewall rule the host applies to its own input. So each one names
loopback unless `.env` says otherwise, and what is open to the network on
purpose is listed here.
"""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

#: Each compose file, and the name of its app service.
APPS = {"compose.yaml": "wactorz-python", "compose.dev.yaml": "wactorz"}

#: Home Assistant runs privileged for its own hardware access and is sized by
#: whoever runs it; its container is not one this project can put a number on.
UNLIMITED = {"homeassistant"}

#: The app's ceilings, the variable that sets each, and the default.
APP_LIMITS = {
    "mem_limit": ("WACTORZ_MEM_LIMIT", "8g"),
    "pids_limit": ("WACTORZ_PIDS_LIMIT", "4096"),
    "cpus": ("WACTORZ_CPUS", "0"),
}

#: Published to the network by default, on purpose: the broker's TLS port is
#: how a node on another machine reaches it.
OPEN_TO_THE_NETWORK = {("compose.yaml", "mosquitto", "8883")}

_VARIABLE = re.compile(r"\$\{(\w+):-([^}]*)\}")


def _services(name: str) -> dict[str, dict]:
    # BaseLoader keeps every scalar as written, so `${NAME:-default}` is compared
    # as text whether or not it is quoted.
    compose = yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    return compose["services"]


def _defaults(value: str) -> str:
    """``value`` as compose resolves it when `.env` sets none of its variables."""
    return _VARIABLE.sub(lambda match: match.group(2), value)


def _limited() -> list[tuple[str, str]]:
    return [
        (name, service) for name in APPS for service in _services(name) if service not in UNLIMITED
    ]


def _published() -> list[tuple[str, str, str]]:
    return [
        (name, service, port)
        for name in APPS
        for service, definition in _services(name).items()
        for port in definition.get("ports", [])
    ]


@pytest.mark.parametrize(("name", "service"), _limited())
def test_every_service_has_a_memory_and_a_process_ceiling(name: str, service: str) -> None:
    definition = _services(name)[service]
    assert definition.get("mem_limit"), f"{name}: {service} has no mem_limit"
    assert definition.get("pids_limit"), f"{name}: {service} has no pids_limit"


@pytest.mark.parametrize(("name", "app"), APPS.items())
@pytest.mark.parametrize(("limit", "setting"), APP_LIMITS.items())
def test_the_apps_ceilings_are_settings(
    name: str, app: str, limit: str, setting: tuple[str, str]
) -> None:
    variable, default = setting
    assert _services(name)[app][limit] == f"${{{variable}:-{default}}}"


def test_both_stacks_set_the_same_ceilings() -> None:
    # The development stack is where a ceiling that is too low should show up.
    def ceilings(name: str) -> dict[str, tuple[str, str]]:
        return {
            "app" if service == APPS[name] else service: (
                definition.get("mem_limit", ""),
                definition.get("pids_limit", ""),
            )
            for service, definition in _services(name).items()
        }

    assert ceilings("compose.yaml") == ceilings("compose.dev.yaml")


@pytest.mark.parametrize(("name", "service", "port"), _published())
def test_a_port_is_published_to_this_host_only(name: str, service: str, port: str) -> None:
    resolved = _defaults(port)
    container_port = resolved.rsplit(":", 1)[-1]
    if (name, service, container_port) in OPEN_TO_THE_NETWORK:
        return
    assert resolved.startswith("127.0.0.1:"), (
        f"{name}: {service} publishes {port!r} on every interface by default; put "
        "127.0.0.1 (or a variable that defaults to it) in front"
    )


def test_what_is_open_on_purpose_is_still_there() -> None:
    # An entry nothing matches would be an exemption waiting for a port to reuse it.
    published = {
        (name, service, _defaults(port).rsplit(":", 1)[-1]) for name, service, port in _published()
    }
    assert published >= OPEN_TO_THE_NETWORK
