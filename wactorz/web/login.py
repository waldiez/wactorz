"""The sign-in page, and the two routes behind it.

Server-rendered on purpose. The dashboard bundle is behind the gate, so the
login form cannot be part of it — and a form that posts to the server keeps the
credential out of JavaScript entirely: nothing here is readable by script, and
the cookie that comes back is `HttpOnly`.

Not a user database. There is one shared key, so "log in" means "prove you know
it once, and hold a revocable session afterwards" rather than identifying
anybody. That is why login-CSRF barely applies (logged in as the attacker and
logged in are the same state) and why the origin gate is enough — see
`auth.UNGUARDED_PATHS`.
"""

from __future__ import annotations

import hmac
import logging

from aiohttp import web

from . import auth, sessions, throttle

logger = logging.getLogger(__name__)

Response = web.Response | web.StreamResponse

#: The page, wearing the dashboard's own clothes.
#:
#: Values are the ones in `frontend/src/styles/base.css` rather than something
#: close to them — the same ink, the same glass panel, the same typeface stack —
#: because this is the first screen of the product, not a utility page bolted to
#: the side of it. The mark is the real `/favicon.svg`, which is why that path
#: is exempt from the key check.
#:
#: Self-contained on purpose: the dashboard bundle is behind the gate, so
#: nothing here may depend on it. No webfont either, matching the dashboard,
#: which declares Inter and lets the system substitute.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Wactorz</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    min-height: 100dvh; display: grid; place-items: center; padding: 1.5rem;
    font-family: "Inter", "Segoe UI", system-ui, sans-serif;
    color: #e0e8ff;
    background:
      radial-gradient(60rem 40rem at 20% -10%, rgba(129, 140, 248, .16), transparent 60%),
      radial-gradient(50rem 35rem at 95% 110%, rgba(103, 232, 249, .12), transparent 60%),
      #050818;
  }}
  main {{
    width: min(23rem, 100%);
    background: rgba(10, 15, 40, .72);
    backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
    border: 1px solid rgba(100, 160, 255, .18); border-radius: 12px;
    padding: 2rem 1.75rem;
    box-shadow: 0 1.5rem 3rem rgba(0, 0, 0, .45);
  }}
  .mark {{ display: block; width: 3rem; height: 3rem; margin: 0 auto .9rem; }}
  h1 {{
    font-size: 1.35rem; font-weight: 600; text-align: center; letter-spacing: .01em;
  }}
  .sub {{
    text-align: center; font-size: .8125rem; line-height: 1.5;
    color: rgba(224, 232, 255, .62); margin: .35rem 0 1.4rem;
  }}
  .sub.error {{ color: #fca5a5; }}
  label {{
    display: block; font-size: .75rem; letter-spacing: .04em; text-transform: uppercase;
    color: rgba(224, 232, 255, .55); margin-bottom: .4rem;
  }}
  input {{
    width: 100%; font: inherit; padding: .65rem .75rem; border-radius: 8px;
    color: #e0e8ff; background: rgba(4, 8, 24, .75);
    border: 1px solid rgba(100, 160, 255, .22);
  }}
  input:focus-visible {{
    outline: none; border-color: rgba(129, 140, 248, .8);
    box-shadow: 0 0 0 3px rgba(129, 140, 248, .22);
  }}
  button {{
    width: 100%; font: inherit; font-weight: 600; margin-top: 1rem; cursor: pointer;
    padding: .65rem .75rem; border: 0; border-radius: 8px; color: #050818;
    background: linear-gradient(135deg, #a5b4fc, #67e8f9);
  }}
  button:hover {{ filter: brightness(1.08); }}
  button:focus-visible {{
    outline: none; box-shadow: 0 0 0 3px rgba(103, 232, 249, .35);
  }}
  @media (prefers-reduced-motion: reduce) {{
    * {{ animation-duration: .01ms !important; transition-duration: .01ms !important; }}
  }}
</style>
</head>
<body>
<main>
  <img class="mark" src="/favicon.svg" alt="" width="48" height="48">
  <h1>Wactorz</h1>
  <p class="sub {message_class}">{message}</p>
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{next_value}">
    <label for="key">API key</label>
    <input id="key" type="password" name="key" autocomplete="current-password"
           autofocus required>
    <button type="submit">Sign in</button>
  </form>
</main>
</body>
</html>
"""


def _wait_message(seconds: float) -> str:
    """How long to wait, in words a person can act on rather than a raw count."""
    if seconds < 60:
        return f"Too many attempts. Try again in {max(1, round(seconds))} seconds."
    return f"Too many attempts. Try again in {round(seconds / 60)} minutes."


def _escape(value: str) -> str:
    """Minimal HTML-attribute escaping for the one value echoed back.

    `next` is caller-supplied and lands in an attribute, so it is escaped even
    though `safe_next` has already reduced it to a local path — the two guards
    answer different questions, and the one that stops a redirect is not the one
    that stops a quote from ending the attribute.
    """
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _page(next_value: str, message: str, status: int = 200) -> web.Response:
    return web.Response(
        # Anything but 200 is the page saying no, so the message carries the
        # colour rather than a separate flag the two callers could disagree on.
        text=_PAGE.format(
            next_value=_escape(next_value),
            message=message,
            message_class="" if status == 200 else "error",
        ),
        content_type="text/html",
        status=status,
        # A login page a proxy has cached, or a browser re-shows from history
        # after signing out, is a page that lies about the state of the server.
        headers={"Cache-Control": "no-store"},
    )


async def login_page_handler(request: web.Request) -> Response:
    """The form — or the end of the startup link, if one was followed here.

    ⚠ Redeeming a code makes this a state-changing GET, the one exception to the
    rule the origin gate leans on ("every GET is a read", which is what makes it
    safe to allow requests carrying no `Origin`). It is written down rather than
    left to contradict that rule silently. What keeps it narrow: the code is
    unguessable, printed to a terminal and nowhere else, expires in minutes, and
    is consumed on first use — so the worst a replay achieves is a login form.
    """
    from ..config import CONFIG  # read per request so a test can swap it

    target = auth.safe_next(request.query.get("next", "/"))

    code = request.query.get("code", "")
    if code and sessions.codes.redeem(code):
        response = web.HTTPFound(target)
        _set_session_cookie(request, response, sessions.store.create())
        logger.info("[auth] signed in with the startup link")
        raise response

    if auth.is_authorized(request, CONFIG.api_key):
        raise web.HTTPFound(target)
    message = (
        "That sign-in link has been used or has expired."
        if code
        else ("Enter the API key to continue.")
    )
    return _page(target, message)


async def login_submit_handler(request: web.Request) -> Response:
    """Check the key, start a session, and put the browser back where it was.

    ⚠ The origin gate has already run — it is middleware, and this is a handler.
    That ordering is load-bearing rather than incidental: a page on another site
    can make a visitor's browser POST here, and if those attempts reached the
    counter they would spend *the visitor's* allowance. Someone else's page
    could then lock a user out of their own dashboard, which turns the
    protection into the attack. Cross-origin attempts are refused upstream and
    never counted here.
    """
    from ..config import CONFIG  # read per request so a test can swap it

    form = await request.post()
    presented = str(form.get("key", ""))
    target = auth.safe_next(str(form.get("next", "/")))
    caller = request.remote or "an unknown address"

    throttle.throttle.prune()
    waiting = throttle.throttle.retry_after(caller)
    if waiting > 0:
        # Refused before the comparison happens at all, so being locked out
        # tells the caller nothing about the key.
        logger.warning("[auth] sign-in refused, %s must wait %.0fs", caller, waiting)
        return _page(target, _wait_message(waiting), status=429)

    if not CONFIG.api_key or not hmac.compare_digest(presented, CONFIG.api_key):
        # No detail about which part was wrong, and the same page either way.
        throttle.throttle.record_failure(caller)
        logger.warning("[auth] failed sign-in from %s", caller)
        return _page(target, "That key was not accepted.", status=401)

    # Built, then raised: the cookie has to be on the redirect that carries it,
    # and aiohttp wants an HTTPException raised rather than returned.
    throttle.throttle.record_success(caller)
    response = web.HTTPFound(target)
    _set_session_cookie(request, response, sessions.store.create())
    logger.info("[auth] signed in from %s", caller)
    raise response


async def logout_handler(request: web.Request) -> Response:
    """End this browser's session and return to the form."""
    sessions.store.revoke(request.cookies.get(auth.SESSION_COOKIE, ""))
    response = web.HTTPFound("/login")
    response.del_cookie(auth.SESSION_COOKIE, path="/")
    raise response


async def logout_everywhere_handler(request: web.Request) -> Response:
    """End every session on every device.

    The reason the cookie holds a session id rather than the key: signing
    everyone out is this, and not a credential change that would also break
    every script, probe and integration at the same moment.
    """
    ended = sessions.store.revoke_all()
    logger.info("[auth] all sessions ended (%d)", ended)
    response = web.HTTPFound("/login")
    response.del_cookie(auth.SESSION_COOKIE, path="/")
    raise response


def sign_in_line(port: int) -> str | None:
    """A one-time sign-in link for the startup banner, or None if none is needed.

    None when no key is configured: there is nothing to sign in to, and offering
    a link would suggest otherwise.

    The caller must `print` this. It is a credential, and the log file is
    unrotated and readable from the dashboard's own log view — a sign-in link
    written there outlives the fifteen minutes it is supposed to exist for.
    """
    from ..config import CONFIG  # read per request so a test can swap it

    if not CONFIG.api_key:
        return None
    return f"Sign in     http://localhost:{port}/login?code={sessions.codes.issue()}"


def _set_session_cookie(request: web.Request, response: web.Response, session_id: str) -> None:
    """Attach the session cookie with the flags that make it worth having.

    `httponly` because the whole point is that the credential is not reachable
    from script. `samesite="Lax"` rather than `Strict`: the one-time sign-in
    link is a top-level navigation from outside the page, and `Strict` withholds
    the cookie on exactly that, so the link would appear to do nothing.

    `secure` only over HTTPS. Setting it unconditionally would break every
    install this is written for — a dashboard on `http://localhost:8888` or a
    LAN address — by making the browser drop the cookie it was just given.
    """
    response.set_cookie(
        auth.SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="Lax",
        path="/",
        max_age=int(sessions.SESSION_TTL_SECONDS),
        secure=request.scheme == "https",
    )
