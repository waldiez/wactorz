"""Who a browser may act on this server's behalf for.

Two checks that fail together. `Origin` identifies the page making the request;
`Host` identifies the name it arrived under, and is what a DNS rebinding attack
gets wrong. Rebinding makes the attacker's page and the address it resolves to
the same name, so Origin and Host agree — the origin check alone passes while
the request reaches a loopback server.
"""

from collections.abc import Iterator
from typing import ClassVar
from unittest.mock import patch

import pytest

from wactorz import config
from wactorz.web.origins import (
    cors_headers,
    host_allowed,
    log_mode,
    normalize_origin,
    origin_allowed,
    parse_allow_list,
    parse_host_list,
    refuse,
    request_origin,
)


class _Url:
    def __init__(self, origin: str) -> None:
        self._origin = origin

    def origin(self) -> str:
        return self._origin


class _Request:
    """Only the two things the rules read."""

    def __init__(self, origin: str = "http://localhost:8888", **headers: str) -> None:
        self.url = _Url(origin)
        self.remote: str | None = "172.30.32.1"
        # Read when a refusal is logged, so a double without it turns a real
        # refusal into an AttributeError.
        self.method = "GET"
        self.path = "/api/feed"
        self.headers = {k.replace("_", "-"): v for k, v in headers.items()}


class TestNormalising:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://Example.COM", "http://example.com"),
            ("http://example.com:80", "http://example.com"),
            ("https://example.com:443", "https://example.com"),
            ("https://example.com:8443", "https://example.com:8443"),
            ("http://example.com/some/path", "http://example.com"),
            ("  http://example.com  ", "http://example.com"),
        ],
    )
    def test_equivalent_spellings_become_one(self, raw: str, expected: str) -> None:
        # Default ports and casing are optional in an Origin header, so a
        # comparison on the raw string rejects callers that are in fact allowed.
        assert normalize_origin(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"), [("ws://x.com", "http://x.com"), ("wss://x.com", "https://x.com")]
    )
    def test_a_websocket_origin_matches_its_page(self, raw: str, expected: str) -> None:
        # A browser sends the *page's* scheme when opening a socket, so an entry
        # written as https must match a wss handshake.
        assert normalize_origin(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "null", "example.com", "not an origin"])
    def test_anything_unidentifiable_is_not_an_origin(self, raw: str) -> None:
        # `null` is a decision, not an oversight: a sandboxed iframe and a
        # file:// page both send it, and neither can be told apart from any
        # other. Treated as a mismatch rather than as same-origin.
        assert normalize_origin(raw) == ""

    def test_an_ipv6_literal_keeps_its_brackets(self) -> None:
        assert normalize_origin("http://[::1]:8888") == "http://[::1]:8888"


class TestWhichOriginTheRequestArrivedAt:
    def test_it_uses_the_address_the_client_reached(self) -> None:
        assert request_origin(_Request("http://localhost:8888")) == "http://localhost:8888"

    def test_a_proxy_header_wins(self) -> None:
        # Behind a reverse proxy the request carries an internal host, so
        # comparing a browser's Origin against it rejects every real call.
        request = _Request(
            "http://172.30.0.5:8888",
            X_Forwarded_Proto="https",
            X_Forwarded_Host="ha.example.com",
        )

        assert request_origin(request) == "https://ha.example.com"

    def test_only_the_first_hop_of_a_forwarded_chain_counts(self) -> None:
        request = _Request(
            "http://internal:8888",
            X_Forwarded_Proto="https, http",
            X_Forwarded_Host="ha.example.com, internal",
        )

        assert request_origin(request) == "https://ha.example.com"


class TestWhichOriginsMayAct:
    def test_the_page_the_server_itself_serves(self) -> None:
        request = _Request("http://localhost:8888")

        assert origin_allowed("http://localhost:8888", request, set())

    def test_a_configured_one(self) -> None:
        request = _Request("http://localhost:8888")

        assert origin_allowed("https://dash.example.com", request, {"https://dash.example.com"})

    def test_anything_else(self) -> None:
        request = _Request("http://localhost:8888")

        assert not origin_allowed("https://evil.example.com", request, set())

    def test_a_null_origin(self) -> None:
        request = _Request("http://localhost:8888")

        assert not origin_allowed("null", request, set())


class TestParsingTheAllowList:
    def test_entries_are_normalised_and_blanks_dropped(self) -> None:
        assert parse_allow_list(" https://A.com:443 , , http://b.com ") == {
            "https://a.com",
            "http://b.com",
        }

    def test_an_unset_list_allows_nobody_extra(self) -> None:
        assert parse_allow_list("") == set()

    def test_host_entries_lose_their_port(self) -> None:
        assert parse_host_list("wactorz.local:8888, Pi.LAN") == {"wactorz.local", "pi.lan"}


class TestWhichHostsWeAnswerTo:
    """The rebinding closure. A rebinding attack needs a name it controls."""

    @pytest.mark.parametrize("host", ["localhost:8888", "localhost", "LOCALHOST:8888"])
    def test_loopback_by_name(self, host: str) -> None:
        assert host_allowed(_Request(Host=host), set())

    @pytest.mark.parametrize("host", ["127.0.0.1:8888", "192.168.1.5:8888", "[::1]:8888"])
    def test_any_ip_literal(self, host: str) -> None:
        # An attacker cannot rebind to an address literal — the page would have
        # to be served from that origin already. Keeps LAN-by-IP access working.
        assert host_allowed(_Request(Host=host), set())

    def test_a_name_nobody_configured_is_refused(self) -> None:
        # This is the rebinding case: the name resolves here, and Origin
        # matches Host, so every other check passes.
        assert not host_allowed(_Request(Host="attacker.example.com"), set())

    def test_a_configured_name_is_accepted(self) -> None:
        # An mDNS or LAN hostname needs listing; that is the cost of the above.
        assert host_allowed(_Request(Host="wactorz.local:8888"), {"wactorz.local"})

    def test_the_forwarded_name_is_the_one_checked(self) -> None:
        # Behind a proxy the direct Host is internal and says nothing about
        # which public name the browser used.
        request = _Request(Host="172.30.0.5:8888", X_Forwarded_Host="ha.example.com")

        assert not host_allowed(request, set())
        assert host_allowed(request, {"ha.example.com"})

    def test_a_request_with_no_host_header_is_not_rejected(self) -> None:
        # HTTP/1.0 and some probes omit it; there is no name to distrust.
        assert host_allowed(_Request(), set())


@pytest.fixture(name="ingress")
def ingress_fixture() -> Iterator[None]:
    """A deployment that is behind Home Assistant's ingress and says so."""
    with patch.object(config, "INGRESS_ENABLED", True):
        yield


class TestTheBypassOnlyExistsWhereIngressDoes:
    def test_a_perfect_ingress_request_is_refused_when_nothing_declared_it(self) -> None:
        # Compose and bare installs have no Supervisor, so no request of theirs
        # should ever take this path. Inferring it from the peer's address is not
        # enough: Docker hands out 172.16-172.31 by default, so an ordinary
        # network can land on the Supervisor's range by coincidence and quietly
        # re-open the bypass on a deployment that has no ingress at all.
        request = _Request(
            "http://172.30.33.2:8888",
            X_Ingress_Path="/api/hassio_ingress/abc123",
            Origin="https://evil.example.com",
        )
        request.remote = "172.30.32.1"

        assert refuse(request, strict_origin=True) is not None


class TestHomeAssistantIngress:
    """The add-on is reachable only through Supervisor, which authenticates first."""

    def test_a_proxied_request_is_answered_whatever_it_arrives_as(self, ingress: None) -> None:
        # Which internal name and scheme Supervisor forwards under is its
        # business. Depending on that is what would 403 the whole panel.
        request = _Request(
            "http://172.30.33.2:8888",
            Host="a0d7b954-wactorz",
            Origin="http://homeassistant.local:8123",
            X_Ingress_Path="/api/hassio_ingress/abc123",
        )

        assert refuse(request) is None
        assert refuse(request, strict_origin=True) is None

    def test_without_the_marker_the_same_request_is_refused(self, ingress: None) -> None:
        # Proves the marker is doing the work, not the rest of the request.
        # A browser cannot set it cross-origin: it is a custom header, so it
        # preflights, and it is not in Allow-Headers.
        request = _Request(
            "http://172.30.33.2:8888",
            Host="a0d7b954-wactorz",
            Origin="http://homeassistant.local:8123",
        )

        assert refuse(request) is not None

    def test_a_malformed_marker_buys_nothing(self, ingress: None) -> None:
        # The header is checked for shape, not merely for presence — it is
        # attacker-suppliable by any peer on the network, and "it is set" would
        # make a garbage value a bypass.
        request = _Request(
            "http://172.30.33.2:8888",
            Host="a0d7b954-wactorz",
            Origin="https://evil.example.com",
            X_Ingress_Path="../../nope",
        )

        assert refuse(request) is not None

    def test_the_marker_is_not_something_a_page_may_send(self) -> None:
        allowed = cors_headers("http://example.com")["Access-Control-Allow-Headers"]

        assert "ingress" not in allowed.lower()


class TestTheIngressPeer:
    """The bypass is only for Supervisor, and only Supervisor's address proves it.

    The header alone cannot: after the add-on's ports were closed, this bypass is
    the one way past the origin and host checks, and any peer on the docker
    network can set a header.
    """

    INGRESS: ClassVar[dict[str, str]] = {"X_Ingress_Path": "/api/hassio_ingress/abc123"}
    FOREIGN: ClassVar[dict[str, str]] = {
        "Origin": "https://evil.example.com",
        "Host": "a0d7b954-wactorz",
    }

    def test_supervisor_gets_the_bypass(self, ingress: None) -> None:
        request = _Request("http://172.30.33.2:8888", **self.INGRESS, **self.FOREIGN)
        request.remote = "172.30.32.1"

        assert refuse(request, strict_origin=True) is None

    def test_the_same_request_from_elsewhere_does_not(self, ingress: None) -> None:
        # The case this whole check exists for: identical headers, different
        # peer. Everything the attacker controls is the same; only the address
        # differs.
        request = _Request("http://172.30.33.2:8888", **self.INGRESS, **self.FOREIGN)
        request.remote = "192.168.1.50"

        assert refuse(request, strict_origin=True) is not None

    def test_a_peer_we_cannot_see_is_not_trusted(self, ingress: None) -> None:
        # A unix socket or an odd transport leaves `remote` unset. Absence is not
        # evidence, so it does not buy the bypass.
        request = _Request("http://172.30.33.2:8888", **self.INGRESS, **self.FOREIGN)
        request.remote = None

        assert refuse(request, strict_origin=True) is not None

    def test_a_malformed_path_from_supervisor_still_buys_nothing(self, ingress: None) -> None:
        # Every gate is required, and they live in different places: the
        # shape check belongs to the accessor, the address to this one.
        request = _Request("http://172.30.33.2:8888", X_Ingress_Path="../../nope", **self.FOREIGN)
        request.remote = "172.30.32.1"

        assert refuse(request, strict_origin=True) is not None

    def test_a_configured_range_is_honoured(self, ingress: None) -> None:
        # The escape hatch: a setup whose proxy sits elsewhere would otherwise
        # have a dead panel and no recourse without a rebuild.
        request = _Request("http://10.1.2.3:8888", **self.INGRESS, **self.FOREIGN)
        request.remote = "10.1.2.9"

        with patch.object(config, "INGRESS_PEERS", "10.1.2.0/24"):
            assert refuse(request, strict_origin=True) is None


class TestSayingWhichModeWeAreIn:
    """Off and "on but never needed" are both an absence of lines otherwise."""

    def test_an_available_bypass_is_announced_at_warning(
        self, ingress: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A deployment that logs only warnings is exactly where someone needs to
        # know that a path past the origin and host checks exists at all.
        with caplog.at_level("WARNING"):
            log_mode()

        assert "ingress mode on" in caplog.text
        assert "172.30.32.0/23" in caplog.text

    def test_an_unavailable_one_says_nothing_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Nothing to warn about, so silence at warning level means "off" — which
        # is what makes the line above readable as a signal.
        with caplog.at_level("WARNING"):
            log_mode()

        assert not caplog.text

    def test_it_is_still_stated_when_the_log_is_verbose(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO"):
            log_mode()

        assert "ingress mode off" in caplog.text

    def test_a_configured_range_is_the_one_announced(
        self, ingress: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"), patch.object(config, "INGRESS_PEERS", "10.1.2.0/24"):
            log_mode()

        assert "10.1.2.0/24" in caplog.text
