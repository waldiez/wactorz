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
from typing import Any, NoReturn
from urllib.parse import quote

from aiohttp import web

from ..config import _env_truthy
from ..core.net import is_loopback
from . import origins, sessions

logger = logging.getLogger(__name__)

#: Reachable without a key, whatever else is configured.
#:
#: `/health` is not a style choice. The container's `HEALTHCHECK` curls it
#: unauthenticated, so guarding it makes every container report unhealthy — and
#: anything acting on that restarts a perfectly healthy process, in a loop.
#: Probes cannot carry a key. The path is exempt, not the method.
#: `/login` is exempt from *authentication* and from nothing else. It stays
#: inside the origin gate, and that is deliberate: C-2 refuses a mismatched
#: `Origin` on state-changing methods, and a browser always sends one on a
#: cross-origin POST — so the origin gate *is* the CSRF control here, with no
#: token and no pre-session cookie to manage.
#: `/favicon.svg` is the mark the sign-in page shows. Gating it would leave that
#: page — the one a stranger is *meant* to reach — asking for a credential to
#: render its own logo. It is a static brand asset, published with the docs.
UNGUARDED_PATHS = frozenset({"/health", "/login", "/favicon.svg"})

#: Carries the session id. `HttpOnly` so script cannot read it — the point of
#: keeping the key server-side is lost if the cookie is scriptable.
SESSION_COOKIE = "wactorz_session"

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
    if sessions.store.is_valid(request.cookies.get(SESSION_COOKIE, "")):
        # A browser that logged in. The cookie holds a session id, never the
        # key, so this path can be revoked without touching the credential
        # every script and probe uses.
        return True
    return hmac.compare_digest(_presented_key(request), api_key)


def wants_html(request: web.Request) -> bool:
    """Whether this is a browser asking for a page rather than a client for data.

    Decides what an unauthenticated caller is told: a person gets sent to the
    login form, a script gets a 401 it can act on. One answer cannot serve
    both — a 302 hands `curl` an HTML page and a 200 it did not earn, and a
    bare 401 leaves a browser on a blank tab with no way forward.
    """
    return "text/html" in request.headers.get("Accept", "")


def safe_next(raw: str) -> str:
    """`raw` if it is somewhere on this server, else the dashboard root.

    Guards both directions of the login round trip — the `?next=` this puts on
    the redirect, and the one that comes back on the form. Without it,
    `?next=https://elsewhere` turns the one page a stranger can always reach
    into an open redirect.

    A scheme-relative `//host` is the case worth naming: it starts with `/`, so
    a "must be absolute" check alone waves it through, and a browser reads it as
    another host. Anything unacceptable becomes `/` rather than being repaired —
    landing on the dashboard is a better failure than landing somewhere the user
    did not ask for.
    """
    if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        return raw
    return "/"


def login_redirect(request: web.Request) -> NoReturn:
    """Send a browser to the login form, remembering where it was going.

    Raised rather than returned: aiohttp deprecated returning an
    ``HTTPException``, and a redirect that arrives as a 200-with-a-body is not
    a redirect at all.
    """
    target = request.path_qs
    if target == "/login" or safe_next(target) == "/":
        raise web.HTTPFound("/login")
    raise web.HTTPFound(f"/login?next={quote(target, safe='')}")


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Refuse anything unauthenticated once a key is set."""
    from ..config import CONFIG  # read per request so a test can swap it

    if request.path in UNGUARDED_PATHS or is_authorized(request, CONFIG.api_key):
        return await handler(request)
    if wants_html(request):
        login_redirect(request)
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
