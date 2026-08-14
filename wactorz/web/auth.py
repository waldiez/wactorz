"""API-key enforcement for the monitor, and the rule that refuses to start.

The monitor serves the dashboard, the whole API and the log endpoint, and until
now read `CONFIG.api_key` nowhere at all. Two halves live here:

* **`auth_middleware`** — the key check, applied to every route rather than
 per handler, because nearly every route is registered twice (`/api/x` and a
 bare `/x`) and a per-route decorator guards whichever alias its author
 remembered.
* **`exposure_refusal`** — the fail-closed rule. Binding somewhere reachable
 with no key refuses to start, rather than warning: a warning scrolls past in a
 container log while the operator believes they merely changed an address.

 **A key means no dashboard, deliberately.** The browser holds no credential
until the cookie work lands, so a guarded install serves an honest 401 at the
door rather than a page that loads and then fails every call it makes. Set a key
for API-only installs; leave it unset — with the loopback default — for
dashboard use.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from aiohttp import web

from ..config import _env_truthy
from ..core.net import is_loopback
from . import origins

logger = logging.getLogger(__name__)

#: Reachable without a key, whatever else is configured.
#:
#: `/health` is not a style choice. The container's `HEALTHCHECK` curls it
#: unauthenticated, so guarding it makes every container report unhealthy — and
#: anything acting on that restarts a perfectly healthy process, in a loop.
#: Probes cannot carry a key. The path is exempt, not the method.
UNGUARDED_PATHS = frozenset({"/health"})

#: Declares "the only way in is already authenticated" for a deployment that
#: must bind widely — a container publishing its own ports, or the add-on behind
#: Home Assistant's ingress. A process cannot see its own port mappings, so this
#: cannot be inferred and has to be stated.
EXPOSED_OK_ENV = "WACTORZ_EXPOSED_OK"


def _presented_key(request: web.Request) -> str:
    """The key this request carries, by either accepted route."""
    presented = request.headers.get("X-API-Key", "")
    if presented:
        return presented
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def is_authorized(request: web.Request, api_key: str) -> bool:
    """Whether `request` may proceed.

    Open when no key is configured — that is the default install, protected by
    the loopback bind rather than by a credential. `Authorization: Bearer` is
    accepted alongside `X-API-Key` so scrapers that only speak standard auth
    headers, Prometheus among them, can reach a guarded endpoint.
    """
    if not api_key:
        return True
    if origins.from_supervisor(request):
        # Home Assistant authenticated the user before proxying, and the panel
        # has no way to attach a header. Peer-verified (C-10), so this is not a
        # header anyone on the network can claim.
        return True
    return hmac.compare_digest(_presented_key(request), api_key)


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Refuse anything unauthenticated once a key is set."""
    from ..config import CONFIG  # read per request so a test can swap it

    if request.path in UNGUARDED_PATHS or is_authorized(request, CONFIG.api_key):
        return await handler(request)
    return web.json_response({"error": "Unauthorized"}, status=401)


def exposure_refusal(bind_host: str, api_key: str) -> str | None:
    """Why this process must not start, or None if it may.

    Reachable off-host with no key is the configuration this whole card exists
    to stop shipping. `WACTORZ_EXPOSED_OK=1` is the deliberate opt-out for a
    deployment whose exposure is already handled — the add-on behind ingress,
    or a container that publishes its own ports.
    """
    if is_loopback(bind_host) or api_key or _env_truthy(EXPOSED_OK_ENV):
        return None
    return (
        f"Refusing to start: bound to {bind_host}, which is reachable from the network, "
        "with no API_KEY set — anything that can reach the port could delete agents, "
        "read the chat log and spend your LLM budget. Set API_KEY, or bind to "
        f"127.0.0.1, or set {EXPOSED_OK_ENV}=1 if the only way in is already "
        "authenticated (behind Home Assistant ingress, or a proxy you control)."
    )
