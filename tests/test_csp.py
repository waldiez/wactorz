"""CSP header + per-request nonce plumbing (wactorz.web.static_site)."""

# pylint: disable=missing-function-docstring

import re

from wactorz.web import static_site


def test_csp_includes_nonce_and_key_directives() -> None:
    policy = static_site.csp_policy("TESTNONCE")
    assert "script-src 'self' 'nonce-TESTNONCE'" in policy
    assert "style-src 'self' 'unsafe-inline'" in policy  # dashboard sets inline styles
    assert "connect-src 'self'" in policy
    assert "worker-src 'self' blob:" in policy  # mqtt.js blob-URL worker
    assert "object-src 'none'" in policy
    # 'self' allows the same-origin HA ingress iframe while blocking foreign framing.
    assert "frame-ancestors 'self'" in policy


class _Req:  # pylint: disable=too-few-public-methods
    """Minimal stand-in for the aiohttp request index_handler reads."""

    def __init__(self, ingress: str = "") -> None:
        self.path = "/"
        self.headers = {"X-Ingress-Path": ingress} if ingress else {}


async def test_index_handler_sets_csp_with_matching_nonce() -> None:
    resp = await static_site.index_handler(_Req())  # pyright: ignore[reportArgumentType]
    csp = resp.headers.get("Content-Security-Policy")
    assert csp is not None
    # The nonce in the header is the same one stamped on the injected script.
    nonce_search = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
    assert nonce_search
    nonce = nonce_search.group(1)
    resp_text = getattr(resp, "text")
    assert isinstance(resp_text, str)
    assert f"<script nonce='{nonce}'>" in resp_text
    # Every inline script is nonce-stamped — no bare <script> slips through.
    assert "<script>" not in resp_text
    # Enforcing now (verified on standalone + HA ingress) — not report-only.
    assert resp.headers.get("Content-Security-Policy-Report-Only") is None


async def test_index_handler_nonce_is_per_request() -> None:
    a = await static_site.index_handler(_Req())  # pyright: ignore[reportArgumentType]
    b = await static_site.index_handler(_Req())  # pyright: ignore[reportArgumentType]
    assert a.headers["Content-Security-Policy"] != b.headers["Content-Security-Policy"]
