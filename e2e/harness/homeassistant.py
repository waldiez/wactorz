"""A Home Assistant instance, and the entities a scenario invents on it.

Home Assistant will hold a state for an entity no integration owns: POST one and
it joins the state machine, fires `state_changed` like anything else, and is
forgotten on request. That is what lets a demo run on somebody else's house. It
needs no device, no helper anyone has to create first, and it leaves nothing
behind — which matters, because the instance on the other end is someone's home
rather than a fixture.

**Readings only, and that is not a simplification.** An invented *switch* accepts
`switch.turn_on` with a 200 and does not move, because nothing is behind it to
do the moving. A scenario asserting on one would be asserting on a lie, and
would pass while nothing happened — the exact failure the nursery story's
docstring records. So a scenario here drives a reading and watches the system
react to it; the reacting is observed through the product, not through a device
that was never there.

Everything invented shares one prefix, so a run killed mid-story leaves entities
that are recognisable on sight and safe for anyone to delete.
"""

from __future__ import annotations

import contextlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

#: Shared by every entity this suite creates.
PREFIX = "wactorz_e2e"

_TIMEOUT = 10.0

_DOTENV = Path(__file__).resolve().parents[2] / ".env"


def setting(name: str) -> str:
    """A Home Assistant setting as the *backend* will see it.

    The environment first, then the repository `.env` — which is the order the
    application itself resolves them in, and the reason this is not just
    `os.getenv`. A backend is started with `dict(os.environ)` and fills what is
    missing from `.env` on its own, so a developer who keeps credentials there
    (which is what the project documents) has a backend that can reach Home
    Assistant and a test process that cannot see that it can. Asking the same
    question both ways is what stops `requires_ha` skipping a scenario that
    would have worked.
    """
    found = os.getenv(name)
    if found:
        return found
    if not _DOTENV.is_file():
        return ""
    return (dotenv_values(_DOTENV).get(name) or "").strip()


class HomeAssistantError(AssertionError):
    """A request to the instance that the caller expected to succeed."""

    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.status = status


def configured() -> bool:
    """Whether there is an instance to talk to at all.

    The scenarios that need one are marked `requires_ha`, and the marker reads
    the same two variables, so this and the skip agree by construction.
    """
    return bool(setting("HA_URL") and setting("HA_TOKEN"))


@dataclass(frozen=True)
class Instance:
    """The instance named by `HA_URL` / `HA_TOKEN`."""

    url: str
    token: str

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise HomeAssistantError(method, path, exc.code, exc.read().decode()) from exc
        return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw

    def state_of(self, entity_id: str) -> str | None:
        """The entity's current state, or None where it does not exist."""
        found = self._call("GET", f"/api/states/{entity_id}")
        return found.get("state") if isinstance(found, dict) else None

    def write_state(self, entity_id: str, state: str, **attributes: Any) -> None:
        self._call("POST", f"/api/states/{entity_id}", {"state": state, "attributes": attributes})

    def forget(self, entity_id: str) -> None:
        """Remove an invented entity. Silent where it is already gone."""
        self._call("DELETE", f"/api/states/{entity_id}")


def instance() -> Instance:
    """The configured instance. Call `configured()` first."""
    url, token = setting("HA_URL"), setting("HA_TOKEN")
    if not url or not token:
        raise AssertionError("no Home Assistant configured: set HA_URL and HA_TOKEN")
    return Instance(url=url.rstrip("/"), token=token)


@dataclass(frozen=True)
class Reading:
    """A sensor this suite invented, and the handle that moves it."""

    instance: Instance
    entity_id: str
    unit: str

    def set(self, value: float | str) -> None:
        """Publish a new reading, as a device with that value would."""
        self.instance.write_state(
            self.entity_id,
            str(value),
            unit_of_measurement=self.unit,
            device_class="temperature",
            friendly_name=self.entity_id.split(".", 1)[-1].replace("_", " "),
        )

    @property
    def value(self) -> str | None:
        return self.instance.state_of(self.entity_id)


@contextlib.contextmanager
def invented_reading(slug: str, initial: float | str, unit: str = "°C") -> Iterator[Reading]:
    """A sensor that exists for the length of the story and is then forgotten.

    Removed even when the story fails, because what it would otherwise leave
    behind is a sensor in somebody's house that nothing owns.
    """
    entity_id = f"sensor.{PREFIX}_{slug}"
    reading = Reading(instance=instance(), entity_id=entity_id, unit=unit)
    reading.set(initial)
    try:
        yield reading
    finally:
        reading.instance.forget(entity_id)
