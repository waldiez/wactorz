"""Static asset + docs serving for the monitor.

Serves the built SPA (``static/app``), its public assets, and the rendered docs
site (``static/docs``), plus the CSP policy and Home Assistant ingress-path
injection the dashboard needs.
"""

import logging
import re
import secrets
from pathlib import Path

from aiohttp import web

from .. import config
from . import runtime

logger = logging.getLogger(__name__)

Response = web.Response | web.FileResponse | web.StreamResponse

# This module lives in wactorz/web/, so the package dir is two levels up
# from __file__ (assets ship inside the wheel at wactorz/static/, and sit at
# <repo>/static/ in a source checkout).
_pkg = Path(__file__).parent.parent
_root = _pkg.parent


def _find_dir(*rel: str) -> Path:
    for base in (_pkg, _root):
        p = base.joinpath(*rel)
        if p.is_dir():
            return p
    return _pkg.joinpath(*rel)


FRONTEND_DIST = _find_dir("static", "app")
FRONTEND_PUBLIC = _find_dir("frontend", "public")
DOCS_SITE = _find_dir("static", "docs")


def _with_no_cache(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


#: An ingress path is a URL path the Supervisor proxy prefixes us with, e.g.
#: `/api/hassio_ingress/<token>`. Anything outside this shape is not one.
_INGRESS_PATH_RE = re.compile(r"^/[A-Za-z0-9_\-./]*$")


def ingress_path_of(request: web.Request) -> str:
    """The request's validated `X-Ingress-Path`, or "" if it is not one.

    The header is attacker-suppliable — any peer on the docker network can set
    it — and the value is interpolated into a `<base href>` and into a **nonced**
    `<script>`. A quote in it closes the string literal, so whatever follows
    executes carrying the page's own CSP nonce: the policy permits it because
    the server vouched for it.

    Two gates, both cheap: the deployment must declare ingress at all
    (`WACTORZ_INGRESS`, set by the add-on and nothing else), and the shape must
    be a plain URL path. The origin gate adds the third: the peer's address
    (`origins.from_supervisor`).
    """
    if not config.INGRESS_ENABLED:
        return ""
    raw = request.headers.get("X-Ingress-Path", "").rstrip("/")
    if not raw:
        return ""
    if ".." in raw or not _INGRESS_PATH_RE.match(raw):
        logger.warning("[static] Ignoring malformed X-Ingress-Path: %r", raw[:80])
        return ""
    return raw


def csp_policy(nonce: str) -> str:
    """Build the dashboard's Content-Security-Policy.

    Nonce-based (not hash-based) because the bootstrap script is injected per
    request and its content varies with the ingress path, so a static hash would
    not match under Home Assistant ingress. ``frame-ancestors 'self'`` allows the
    same-origin HA ingress / Nabu Casa remote iframe while blocking foreign framing.
    Verified compliant on both standalone and HA ingress (via a report-only pass)
    before being enforced.
    """
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            # mqtt.js runs its client in a Web Worker created from a blob: URL.
            "worker-src 'self' blob:",
            "object-src 'none'",
            "base-uri 'self'",
            # HA ingress (and Nabu Casa remote) frame the add-on from the same
            # origin, so 'self' allows the iframe while blocking foreign framing.
            "frame-ancestors 'self'",
        )
    )


async def index_handler(request: web.Request) -> Response:
    """Serve the SPA shell with a per-request CSP nonce and the ingress prefix injected."""
    if request.path.endswith("favicon.svg"):
        for candidate in [FRONTEND_PUBLIC / "favicon.svg", FRONTEND_DIST / "favicon.svg"]:
            if candidate.exists():
                return _with_no_cache(web.FileResponse(candidate))

    for candidate in [
        FRONTEND_DIST / "index.html",
        _find_dir("frontend") / "index.html",
    ]:
        if candidate.exists():
            ingress_path = ingress_path_of(request)
            # Per-request nonce for the injected bootstrap script below so the CSP
            # can allow it without 'unsafe-inline'.
            nonce = secrets.token_urlsafe(16)
            # Inject the ingress path so the frontend can prefix all fetch/WS URLs.
            # When not behind ingress, ingress_path is "" and all URLs stay relative.
            inject = (
                f"<script nonce='{nonce}'>window.__WACTORZ_INGRESS_PATH='{ingress_path}';</script>"
            )
            if ingress_path:
                inject = f'<base href="{ingress_path}/">{inject}'

            content = candidate.read_text(encoding="utf-8")
            # Stamp the same nonce on the page's own inline scripts (e.g. the SW
            # registration) so they pass the CSP too. First-party bare `<script>`
            # tags only — the module bundle carries `type=`/`src=`.
            content = content.replace("<script>", f"<script nonce='{nonce}'>")
            content = content.replace("<head>", f"<head>{inject}", 1)
            response = _with_no_cache(web.Response(text=content, content_type="text/html"))
            response.headers["Content-Security-Policy"] = csp_policy(nonce)
            return response
    raise web.HTTPNotFound()


def _within(candidate: Path, base: Path) -> bool:
    """Whether `candidate` is really inside `base`. Both must be resolved.

    ⚠ `str.startswith` is not this test, and that is what was here. With a base
    of `…/static/app`, the path `…/static/app-old/secret` starts with it and
    passes — a sibling whose name merely shares a prefix escapes the directory
    the check exists to pin. `is_relative_to` compares path *components*, so
    only a genuine descendant passes.

    `resolve()` already collapses `..`, so this is the remaining half of the
    guard rather than the whole of it.
    """
    return candidate.is_relative_to(base)


async def static_handler(request: web.Request) -> Response:
    """Serve a built asset, rewriting absolute API/WS URLs when behind HA ingress."""
    rel = request.match_info["path"]

    # Special case for favicon if it's requested at root
    if rel == "favicon.svg":
        for candidate in [FRONTEND_PUBLIC / "favicon.svg", FRONTEND_DIST / "favicon.svg"]:
            if candidate.exists():
                return _with_no_cache(web.FileResponse(candidate))

    ingress_path = ingress_path_of(request)

    for base in [FRONTEND_DIST, FRONTEND_PUBLIC]:
        candidate = base / rel
        try:
            candidate = candidate.resolve()
            if candidate.is_file() and _within(candidate, base.resolve()):
                # If it's a JS file and we're behind Ingress, we must rewrite hardcoded absolute paths
                if candidate.suffix == ".js" and ingress_path:
                    content = candidate.read_text(encoding="utf-8")
                    # Rewrite hardcoded paths from "/api/..." to "api/..." or prepending ingress_path
                    # The frontend seems to use "/api/actors", "/api/config", etc.
                    content = content.replace('"/api/', f'"{ingress_path}/api/')
                    content = content.replace('"/config"', f'"{ingress_path}/config"')
                    content = content.replace('"/actors"', f'"{ingress_path}/actors"')
                    # Point the WebSocket at the monitor's actual port (WS_PORT),
                    # not HA's 8123. WS_PORT is where the /ws proxy lives.
                    content = content.replace(
                        "`ws://${location.host}/ws`",
                        f"`ws://${{location.hostname}}:{runtime.WS_PORT}/ws`",
                    )

                    return _with_no_cache(
                        web.Response(text=content, content_type="application/javascript")
                    )

                return _with_no_cache(web.FileResponse(candidate))
        except Exception:
            pass
    raise web.HTTPNotFound()


async def docs_redirect(_request: web.Request) -> web.Response:
    """Send ``/docs`` to ``/docs/`` so relative asset links resolve.

    Raised rather than returned: aiohttp deprecated returning an
    ``HTTPException`` (aio-libs/aiohttp#2415).
    """
    raise web.HTTPFound("/docs/")


async def docs_handler(request: web.Request) -> web.FileResponse:
    """Serve the rendered docs site, resolving directory URLs to their index page."""
    if not DOCS_SITE.is_dir():
        raise web.HTTPNotFound(
            reason="Docs not built — run: python3 scripts/build_docs.py  (or: make docs-build)"
        )
    rel = request.match_info.get("path", "") or "index.html"
    if not rel or rel.endswith("/"):
        rel += "index.html"
    root = DOCS_SITE.resolve()
    candidate = (DOCS_SITE / rel).resolve()
    if not _within(candidate, root):
        # Refused before either branch below: the directory-index fallback reads
        # `candidate.parent`, so a path that escaped would list a directory
        # outside the docs root and name one of its entries in a redirect.
        raise web.HTTPNotFound()
    try:
        if candidate.is_file():
            return web.FileResponse(candidate)
        if rel.endswith("index.html") and not candidate.exists():
            parent = candidate.parent
            if parent.is_dir():
                for sub in sorted(parent.iterdir()):
                    if sub.is_dir() and (sub / "index.html").exists():
                        raise web.HTTPFound(request.path.rstrip("/") + f"/{sub.name}/index.html")
    except web.HTTPFound:
        raise
    except Exception:
        pass
    raise web.HTTPNotFound()
