"""Liveness and readiness probes, shared by the monitor and the REST interface.

Two different questions, answered at different paths because acting on them
means different things:

* **Liveness** — `/health`, `/healthz`, `/livez`. Is this process able to
  answer at all? A failure here is a request to restart it, so it depends on
  nothing outside the process: an event loop that can run this handler is the
  whole of the evidence. Were it to fail while the broker is down, Docker's
  `HEALTHCHECK` or a Kubernetes liveness probe would restart a healthy process
  in a loop for as long as the broker stayed away, and the restart would fix
  nothing.
* **Readiness** — `/ready`, `/readyz`. Should traffic be sent here right now?
  A failure here takes the process out of rotation until it passes again,
  which is the right answer while it starts, while it stops, and while
  something it cannot work without is missing. It checks only what every
  request relies on — the supervision tree, `main`, the broker link and the
  database — and not agents that talk to Home Assistant or a device, whose
  outages the supervisor already handles and which chat keeps working through.

`/healthz` and the `z` spellings are the Kubernetes convention; the plain ones
are what the container images and compose files already probe. Every path is
reachable without a key, since a probe cannot always carry one, so the bodies
say which check failed and never why in any more detail than that: no
hostnames, paths or exception text reach an unauthenticated caller.
"""

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

from ..core.actor import ActorState
from ..core.persistence import get_db

if TYPE_CHECKING:
    from ..core.registry import ActorSystem

LIVENESS_PATHS = frozenset({"/health", "/healthz", "/livez"})
READINESS_PATHS = frozenset({"/ready", "/readyz"})
PROBE_PATHS = LIVENESS_PATHS | READINESS_PATHS

#: How long the database check waits for the connection lock. A write holding it
#: for longer than this is itself worth taking the process out of rotation for.
DB_PING_TIMEOUT_S = 2.0

#: A probe's answer describes this instant, and a cache that kept it would
#: report a process ready after it stopped being so.
_NO_STORE = {"Cache-Control": "no-store"}

OK = "ok"


async def liveness_handler(_request: web.Request) -> web.Response:
    """200 whenever the process can answer."""
    return web.json_response({"status": "ok"}, headers=_NO_STORE)


def system_checks(system: "ActorSystem") -> dict[str, str]:
    """The in-memory readiness checks for a running actor system, each `ok` or why not."""
    supervisor = system.supervisor
    if system.stopping:
        started = "stopping"
    elif not supervisor.running:
        started = "not started"
    else:
        started = OK

    main = system.registry.find_by_name("main")
    if main is None:
        main_state = "missing"
    elif main.state != ActorState.RUNNING:
        main_state = main.state.value
    else:
        main_state = OK

    return {
        "supervisor": started,
        "main": main_state,
        "broker": broker_check(bool(system.mqtt_status().get("connected"))),
    }


async def database_check() -> str:
    """Whether the process's database answers a query, off the event loop."""
    db = get_db()
    if db is None:
        return "not open"
    answered = await asyncio.to_thread(db.ping, DB_PING_TIMEOUT_S)
    return OK if answered else "unavailable"


def broker_check(connected: bool) -> str:
    """The broker check's answer for a link that is, or is not, up."""
    return OK if connected else "disconnected"


async def readiness(system: "ActorSystem") -> dict[str, str]:
    """Every readiness check for a process running ``system``, by name."""
    return {**system_checks(system), "database": await database_check()}


def readiness_response(checks: dict[str, str]) -> web.Response:
    """200 when every check passed, 503 naming the checks otherwise."""
    ready = all(value == OK for value in checks.values())
    return web.json_response(
        {"status": "ready" if ready else "not ready", "checks": checks},
        status=200 if ready else 503,
        headers=_NO_STORE,
    )
