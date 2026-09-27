"""The manual agent fetches only public web addresses, and only so much.

Its candidate URLs come from search results and from links inside the pages it
fetches, and it runs on a machine inside a private network. A result, or a
redirect from one, pointing at a router's admin page or a cloud metadata address
is fetched as readily as a manual unless every hop is checked; and a response
that keeps streaming is held in memory whole unless the read stops somewhere.

The program is exec'd as it is at spawn, and driven with a stand-in HTTP client
and a stubbed DNS lookup, so nothing here touches the network.
"""

import ipaddress
import socket
from typing import Any

import pytest

from wactorz.catalogue_agents.manual_agent import AGENT_CODE

NS: dict[str, Any] = {}
exec(compile(AGENT_CODE, "manual_agent<AGENT_CODE>", "exec"), NS)

#: Host names the stubbed lookup knows, and the addresses each resolves to.
DNS: dict[str, list[str]] = {
    "manuals.example": ["93.184.216.34"],
    "cdn.example": ["2606:2800:220:1::1"],
    "router.lan": ["192.168.1.1"],
    "metadata.cloud": ["169.254.169.254"],
    "localhost": ["127.0.0.1"],
    "split.example": ["93.184.216.34", "10.0.0.5"],
    "linklocal6.example": ["fe80::1%eth0"],
}


@pytest.fixture(autouse=True)
def _dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host: str, _port: Any, *_args: Any, **_kwargs: Any) -> list[Any]:
        # A literal address resolves to itself, as the real lookup does.
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            if host not in DNS:
                raise socket.gaierror("unknown host") from None
            addresses = DNS[host]
        return [(None, None, None, "", (address, 0)) for address in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class _Agent:
    def __init__(self) -> None:
        self.logged: list[str] = []

    async def log(self, text: str) -> None:
        self.logged.append(text)


class _Response:
    def __init__(self, status: int = 200, body: bytes = b"", location: str = "") -> None:
        self.status_code = status
        self.is_redirect = bool(location)
        self.headers = {"location": location, "content-type": "application/pdf"}
        self._body = body

    async def aiter_bytes(self) -> Any:
        for start in range(0, len(self._body), 1024):
            yield self._body[start : start + 1024]


class _Stream:
    def __init__(self, response: _Response) -> None:
        self._response = response

    async def __aenter__(self) -> _Response:
        return self._response

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


class _Client:
    """Answers each URL from a table, recording what was requested."""

    def __init__(self, responses: dict[str, _Response]) -> None:
        self._responses = responses
        self.requested: list[str] = []

    def stream(self, _method: str, url: str) -> _Stream:
        self.requested.append(url)
        return _Stream(self._responses[url])


class TestWhichAddressesArePublic:
    @pytest.mark.parametrize(
        "url",
        ["https://manuals.example/m.pdf", "http://cdn.example/m.pdf"],
    )
    def test_a_public_host_is_fetched(self, url: str) -> None:
        assert NS["_public_address"](url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://router.lan/admin",
            "http://metadata.cloud/latest/meta-data/",
            "http://localhost:8000/api",
            "http://127.0.0.1/",
            "http://[::1]/",
            "http://linklocal6.example/",
            # One private address among public ones is enough to refuse.
            "http://split.example/m.pdf",
        ],
    )
    def test_a_private_host_is_not(self, url: str) -> None:
        assert NS["_public_address"](url) is False

    @pytest.mark.parametrize(
        "url", ["ftp://manuals.example/m.pdf", "file:///etc/passwd", "https:///m.pdf"]
    )
    def test_only_http_and_https_with_a_host(self, url: str) -> None:
        assert NS["_public_address"](url) is False

    def test_a_public_literal_address_is_fetched(self) -> None:
        assert NS["_public_address"]("http://93.184.216.34/m.pdf") is True

    def test_a_host_that_does_not_resolve_is_not(self) -> None:
        assert NS["_public_address"]("https://nowhere.example/m.pdf") is False


class TestFetching:
    async def test_a_public_pdf_is_fetched(self) -> None:
        client = _Client({"https://manuals.example/m.pdf": _Response(body=b"%PDF-1.7 ...")})

        fetched = await NS["_fetch_public"](_Agent(), client, "https://manuals.example/m.pdf")

        assert fetched == (200, "application/pdf", b"%PDF-1.7 ...")

    async def test_a_redirect_to_a_private_address_is_not_followed(self) -> None:
        client = _Client(
            {"https://manuals.example/m.pdf": _Response(302, location="http://router.lan/admin")}
        )
        agent = _Agent()

        fetched = await NS["_fetch_public"](agent, client, "https://manuals.example/m.pdf")

        assert fetched is None
        assert client.requested == ["https://manuals.example/m.pdf"]
        assert "not a public web address" in agent.logged[-1]

    async def test_a_relative_redirect_is_followed_on_the_same_host(self) -> None:
        client = _Client(
            {
                "https://manuals.example/m": _Response(301, location="/files/m.pdf"),
                "https://manuals.example/files/m.pdf": _Response(body=b"%PDF"),
            }
        )

        fetched = await NS["_fetch_public"](_Agent(), client, "https://manuals.example/m")

        assert fetched is not None and fetched[2] == b"%PDF"

    async def test_redirects_end_somewhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(NS, "MAX_REDIRECTS", 2)
        loop = {"https://manuals.example/a": _Response(302, location="https://manuals.example/a")}
        agent = _Agent()

        fetched = await NS["_fetch_public"](agent, _Client(loop), "https://manuals.example/a")

        assert fetched is None
        assert "redirects" in agent.logged[-1]

    async def test_a_download_that_keeps_going_is_abandoned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(NS, "MAX_DOWNLOAD_BYTES", 4096)
        client = _Client({"https://manuals.example/big.pdf": _Response(body=b"x" * 10_000)})
        agent = _Agent()

        fetched = await NS["_fetch_public"](agent, client, "https://manuals.example/big.pdf")

        assert fetched is None
        assert "larger than" in agent.logged[-1]
