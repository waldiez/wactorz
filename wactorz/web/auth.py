# SPDX-License-Identifier: Apache-2.0
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

import hmac
import logging
import math
from typing import Any, NoReturn
from urllib.parse import quote

from aiohttp import web

from ..config import _env_truthy
from ..core.net import is_loopback
from . import origins, probes, sessions, throttle

logger: logging.Logger = logging.getLogger(__name__)

#: Reachable without a key, whatever else is configured.
#:
#: The probes (`probes.PROBE_PATHS`) are not a style choice. The container's
#: `HEALTHCHECK` curls `/health` unauthenticated, so guarding it makes every
#: container report unhealthy — and anything acting on that restarts a perfectly
#: healthy process, in a loop. Probes cannot always carry a key, and they answer
#: with nothing a stranger could use. The paths are exempt, not the method.
#: `/login` is exempt from *authentication* and from nothing else. It stays
#: inside the origin gate, and that is deliberate: C-2 refuses a mismatched
#: `Origin` on state-changing methods, and a browser always sends one on a
#: cross-origin POST — so the origin gate *is* the CSRF control here, with no
#: token and no pre-session cookie to manage.
#: `/favicon.svg` is the mark the sign-in page shows. Gating it would leave that
#: page — the one a stranger is *meant* to reach — asking for a credential to
#: render its own logo. It is a static brand asset, published with the docs.
UNGUARDED_PATHS = probes.PROBE_PATHS | {"/login", "/favicon.svg"}

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


def key_matches(presented: str, api_key: str) -> bool:
    """Whether `presented` is the key, compared in constant time.

    On bytes, because `compare_digest` raises on a string that is not ASCII, and
    a header or a form field holds whatever the caller put there.
    """
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogateescape"),
        api_key.encode("utf-8", "surrogateescape"),
    )


def needs_no_key(request: web.Request, api_key: str) -> bool:
    """Whether `request` is let in without presenting the key.

    Open when no key is configured — that is the default install, protected by
    the loopback bind rather than by a credential.
    """
    # Home Assistant authenticated the user before proxying, and the panel has no
    # way to attach a header. Peer-verified (C-10), so this is not a header anyone
    # on the network can claim. A browser that logged in holds a session id, never
    # the key, so that path can be revoked without touching the credential every
    # script and probe uses.
    return (
        not api_key
        or origins.from_supervisor(request)
        or sessions.store.is_valid(request.cookies.get(SESSION_COOKIE, ""))
    )


def is_authorized(request: web.Request, api_key: str) -> bool:
    """Whether `request` may proceed, with no attempt counted.

    `Authorization: Bearer` is accepted alongside `X-API-Key` so scrapers that
    only speak standard auth headers, Prometheus among them, can reach a guarded
    endpoint. Anything reachable without a key must not call this with a caller's
    header in hand: it would answer a guess uncounted. `auth_middleware` is the
    throttled path.
    """
    return needs_no_key(request, api_key) or key_matches(_presented_key(request), api_key)


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
    another host. So is one hidden behind a control character: a browser drops
    tabs and newlines from a URL before reading it, and aiohttp drops them from
    a `Location` too, so `/<tab>/host` arrives as `//host`. Anything
    unacceptable becomes `/` rather than being repaired — landing on the
    dashboard is a better failure than landing somewhere the user did not ask
    for.
    """
    if any(ch < " " or ch == "\x7f" for ch in raw):
        return "/"
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


def _too_many_attempts(waiting: float) -> web.Response:
    """The answer to a caller that must wait, saying for how long."""
    return web.json_response(
        {"error": "Too many failed attempts"},
        status=429,
        headers={"Retry-After": str(math.ceil(waiting))},
    )


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Refuse anything unauthenticated once a key is set.

    A key in a header spends the same allowance per address as the sign-in form,
    since either answers whether a guess was right. Only a request that presents
    a key is counted: a browser whose session ended arrives with none, and has to
    reach the form. A session or Home Assistant's ingress passes before this, so a
    script with a stale key beside a signed-in browser does not lock the browser out.
    """
    from ..config import CONFIG  # read per request so a test can swap it

    if request.path in UNGUARDED_PATHS or needs_no_key(request, CONFIG.api_key):
        return await handler(request)
    presented = _presented_key(request)
    if presented:
        caller = origins.client_address(request) or throttle.UNKNOWN_ADDRESS
        waiting = throttle.throttle.retry_after(caller)
        if waiting > 0:
            # Refused before the comparison, as on the form, so a locked-out
            # caller learns nothing about the key, whether it is right or wrong.
            logger.debug("[auth] request refused, %s must wait %.0fs", caller, waiting)
            return _too_many_attempts(waiting)
        if key_matches(presented, CONFIG.api_key):
            throttle.throttle.record_success(caller)
            return await handler(request)
        throttle.throttle.prune()
        throttle.throttle.record_failure(caller)
        logger.warning("[auth] wrong key from %s", caller)
    if wants_html(request):
        login_redirect(request)
    return web.json_response({"error": "Unauthorized"}, status=401)


#: The shortest configured key this will not complain about. A generated one is
#: 64 hex characters; this is the length below which a key stops being something
#: an attacker has to work for. Not a refusal — a short key on an install that
#: has one at all is still far better than the alternative, and the throttle is
#: what actually stands between it and a guesser.
MIN_KEY_LENGTH = 16


def weak_key_warning(api_key: str) -> str | None:
    """What to say at startup about a key short enough to guess, or None.

    Says what protects them in the meantime rather than only what is wrong:
    wrong keys back off and then lock out, so a short key is slow to attack
    rather than open. The limit is per address, so many addresses get as many
    tries, which is the reason to fix it rather than rely on the throttle.
    """
    if not api_key or len(api_key) >= MIN_KEY_LENGTH:
        return None
    return (
        f"API_KEY is {len(api_key)} characters, under the {MIN_KEY_LENGTH} this expects. "
        "Wrong keys are throttled, so guessing it from one address is slow — but the "
        "limit is per address, so a short one is worth replacing. "
        "Something like `openssl rand -hex 32` gives a key nobody has to remember."
    )


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
