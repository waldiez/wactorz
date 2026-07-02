"""Report-only CSP header + per-request nonce plumbing (monitor_server)."""

import importlib
import re
import sys

import wactorz.monitor_server as m


def _ensure_real_aiohttp():
    """Restore the genuine aiohttp package.

    Another suite test replaces ``sys.modules['aiohttp']`` with a partial stub;
    index_handler does ``from aiohttp import web`` at call time, so without this it
    could pick up that stub. Re-importing from disk yields the real module.
    """
    for name in [n for n in list(sys.modules) if n == "aiohttp" or n.startswith("aiohttp.")]:
        del sys.modules[name]
    importlib.import_module("aiohttp.web")


def test_csp_report_only_includes_nonce_and_key_directives():
    policy = m._csp_report_only("TESTNONCE")
    assert "script-src 'self' 'nonce-TESTNONCE'" in policy
    assert "style-src 'self' 'unsafe-inline'" in policy  # dashboard sets inline styles
    assert "connect-src 'self'" in policy
    assert "worker-src 'self' blob:" in policy  # mqtt.js blob-URL worker
    assert "object-src 'none'" in policy
    # Omitted on purpose so the Home Assistant ingress iframe is never flagged.
    assert "frame-ancestors" not in policy


class _Req:
    """Minimal stand-in for the aiohttp request index_handler reads."""

    def __init__(self, ingress: str = ""):
        self.path = "/"
        self.headers = {"X-Ingress-Path": ingress} if ingress else {}


async def test_index_handler_sets_report_only_csp_with_matching_nonce():
    _ensure_real_aiohttp()
    resp = await m.index_handler(_Req())
    csp = resp.headers.get("Content-Security-Policy-Report-Only")
    assert csp is not None
    # The nonce in the header is the same one stamped on the injected script.
    nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp).group(1)
    assert f"<script nonce='{nonce}'>" in resp.text
    # Every inline script is nonce-stamped — no bare <script> slips through.
    assert "<script>" not in resp.text
    # Enforcing CSP is NOT set (report-only only) until verified in a browser.
    assert resp.headers.get("Content-Security-Policy") is None


async def test_index_handler_nonce_is_per_request():
    _ensure_real_aiohttp()
    a = await m.index_handler(_Req())
    b = await m.index_handler(_Req())
    assert (
        a.headers["Content-Security-Policy-Report-Only"]
        != b.headers["Content-Security-Policy-Report-Only"]
    )
