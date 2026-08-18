"""The `X-Ingress-Path` header decides how the page addresses its own backend.

Home Assistant's Supervisor sets it when proxying the add-on. Any peer on the
docker network can set it too, and the value is interpolated into a `<base
href>` and into a **nonced** `<script>` — so a quote in it closes the string
literal and whatever follows executes carrying the page's own CSP nonce, which
the policy permits because the server vouched for it.
"""

import pytest

from wactorz.web import static_site


class _Request:
    """Just enough of `web.Request` for the header read under test."""

    def __init__(self, **headers: str) -> None:
        self.headers = headers


class TestIngressPath:
    """The value lands in a `<base href>` and inside a nonced `<script>`."""

    def test_a_real_ingress_path_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The shape check runs only where the deployment declares ingress at
        # all — the bypass does not exist on a plain Docker or bare install.
        monkeypatch.setattr("wactorz.config.INGRESS_ENABLED", True)
        req = _Request(**{"X-Ingress-Path": "/api/hassio_ingress/abc123"})

        assert static_site.ingress_path_of(req) == "/api/hassio_ingress/abc123"  # type: ignore[arg-type]

    def test_no_header_is_empty(self) -> None:
        assert static_site.ingress_path_of(_Request()) == ""  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "hostile",
        [
            "/x';window.__pwned=1;//",  # closes the JS string literal
            '/x"><script>alert(1)</script>',  # closes the <base href> attribute
            "/x\n<script>alert(1)</script>",  # newline into the injected markup
            "/api/../../etc",  # traversal
            "not-a-path",  # no leading slash
        ],
    )
    def test_anything_that_is_not_a_path_is_refused(self, hostile: str) -> None:
        # Refused rather than escaped: the header is attacker-suppliable, and a
        # value that is not a URL path is not a mangled ingress path — it is
        # someone else's. Escaping would preserve it; dropping it says no.
        assert static_site.ingress_path_of(_Request(**{"X-Ingress-Path": hostile})) == ""  # type: ignore[arg-type]

    def test_the_injected_script_cannot_be_broken_out_of(self) -> None:
        hostile = "/x';window.__pwned=1;//"
        path = static_site.ingress_path_of(_Request(**{"X-Ingress-Path": hostile}))  # type: ignore[arg-type]

        # This is the interpolation the handler performs. With the value
        # dropped, the literal stays closed.
        injected = f"window.__WACTORZ_INGRESS_PATH='{path}';"
        assert injected == "window.__WACTORZ_INGRESS_PATH='';"
