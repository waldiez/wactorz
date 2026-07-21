"""Static asset + docs serving for the monitor.

Serves the built SPA (``static/app``), its public assets, and the rendered docs
site (``static/docs``), plus the CSP policy and Home Assistant ingress-path
injection the dashboard needs.
"""

import secrets
from pathlib import Path

from aiohttp import web

from . import runtime

Response = web.Response | web.FileResponse | web.StreamResponse

# This module lives in wactorz/monitor/, so the package dir is two levels up
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
    if request.path.endswith("favicon.svg"):
        for candidate in [FRONTEND_PUBLIC / "favicon.svg", FRONTEND_DIST / "favicon.svg"]:
            if candidate.exists():
                return _with_no_cache(web.FileResponse(candidate))

    for candidate in [
        FRONTEND_DIST / "index.html",
        _find_dir("frontend") / "index.html",
    ]:
        if candidate.exists():
            ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
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


async def static_handler(request: web.Request) -> Response:
    rel = request.match_info["path"]

    # Special case for favicon if it's requested at root
    if rel == "favicon.svg":
        for candidate in [FRONTEND_PUBLIC / "favicon.svg", FRONTEND_DIST / "favicon.svg"]:
            if candidate.exists():
                return _with_no_cache(web.FileResponse(candidate))

    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")

    for base in [FRONTEND_DIST, FRONTEND_PUBLIC]:
        candidate = base / rel
        try:
            candidate = candidate.resolve()
            if candidate.is_file() and str(candidate).startswith(str(base.resolve())):
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
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    raise web.HTTPNotFound()


async def docs_redirect(_request: web.Request) -> web.HTTPFound:
    return web.HTTPFound("/docs/")


async def docs_handler(request: web.Request) -> web.FileResponse:
    if not DOCS_SITE.is_dir():
        raise web.HTTPNotFound(
            reason="Docs not built — run: python3 scripts/build_docs.py  (or: make docs-build)"
        )
    rel = request.match_info.get("path", "") or "index.html"
    if not rel or rel.endswith("/"):
        rel += "index.html"
    root = DOCS_SITE.resolve()
    candidate = (DOCS_SITE / rel).resolve()
    try:
        if candidate.is_file() and str(candidate).startswith(str(root)):
            return web.FileResponse(candidate)
        if rel.endswith("index.html") and not candidate.exists():
            parent = candidate.parent
            if parent.is_dir():
                for sub in sorted(parent.iterdir()):
                    if sub.is_dir() and (sub / "index.html").exists():
                        raise web.HTTPFound(request.path.rstrip("/") + f"/{sub.name}/index.html")
    except web.HTTPFound:
        raise
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    raise web.HTTPNotFound()
