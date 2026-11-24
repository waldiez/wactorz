# SPDX-License-Identifier: Apache-2.0
"""The manual agent fetches only public web addresses, and only so much.

Its candidate URLs come from search results and from links inside the pages it
fetches, and it runs on a machine inside a private network. A result, or a
redirect from one, pointing at a router's admin page or a cloud metadata address
is fetched as readily as a manual unless every hop is checked; and a response
that keeps streaming is held in memory whole unless the read stops somewhere.

A host is resolved once per hop, and the request is sent to the address that
was checked: resolving it again when connecting would let a name answer with a
public address for the check and a private one for the fetch. Each such
connection is closed after its response, since httpx pools by the address and a
second host there would reuse a connection checked for the first. Through a
proxy the same holds: the proxy is asked for the checked address, so it looks
nothing up, and TLS inside its tunnel names the host.

The program is exec'd as it is at spawn, and driven with a stand-in HTTP client
and a stubbed DNS lookup, so nothing here touches the network.
"""

import asyncio
import datetime
import ipaddress
import re
import socket
import ssl
import sys
import threading
import types
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.programs import program_namespace

NS = program_namespace("manual_agent.py")

#: Host names the stubbed lookup knows, and the addresses each resolves to.
DNS: dict[str, list[str]] = {
    "manuals.example": ["93.184.216.34"],
    "cdn.example": ["2606:2800:220:1::1"],
    "router.lan": ["192.168.1.1"],
    "metadata.cloud": ["169.254.169.254"],
    "localhost": ["127.0.0.1"],
    "split.example": ["93.184.216.34", "10.0.0.5"],
    "linklocal6.example": ["fe80::1%eth0"],
    # "mänuals.example", as it goes on the wire.
    "xn--mnuals-bua.example": ["93.184.216.34"],
}

#: Names that answer with these addresses in turn, one per lookup: a rebinding server.
REBINDING: dict[str, list[str]] = {}


PROXY_VARIABLES = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy unless a test sets one, and none from the system's settings (macOS, Windows)."""
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
    # On every platform at run time; typeshed leaves it out.
    bypass_environment = urllib.request.proxy_bypass_environment  # pyright: ignore[reportAttributeAccessIssue]
    monkeypatch.setattr(urllib.request, "proxy_bypass", bypass_environment)
    # Nor a CA of the machine's own: a test that needs one names it.
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)


@pytest.fixture(autouse=True)
def _dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host: str, _port: Any, *_args: Any, **_kwargs: Any) -> list[Any]:
        # A literal address resolves to itself, as the real lookup does.
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            if host in REBINDING:
                addresses = [REBINDING[host].pop(0)]
            elif host in DNS:
                addresses = DNS[host]
            else:
                raise socket.gaierror("unknown host") from None
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
    """Answers each URL from a table, recording what was asked for and where it was sent.

    A request goes to an address with the host it asks for in its ``Host``
    header; the table is keyed by the URL as asked for.
    """

    def __init__(self, responses: dict[str, _Response]) -> None:
        self._responses = responses
        self.requested: list[str] = []
        #: (the URL connected to, the Host header, the TLS name) of each request.
        self.sent: list[tuple[str, str, str | None]] = []
        self.headers: list[dict[str, str]] = []

    def stream(
        self, _method: str, url: str, *, headers: dict[str, str], extensions: dict[str, Any]
    ) -> _Stream:
        netloc = headers.get("Host") or urllib.parse.urlsplit(url).netloc
        asked = urllib.parse.urlsplit(url)._replace(netloc=netloc).geturl()
        self.requested.append(asked)
        self.sent.append((url, netloc, extensions.get("sni_hostname")))
        self.headers.append(headers)
        return _Stream(self._responses[asked])


class TestWhichAddressesArePublic:
    @pytest.mark.parametrize(
        ("url", "address"),
        [
            ("https://manuals.example/m.pdf", "93.184.216.34"),
            ("http://cdn.example/m.pdf", "2606:2800:220:1::1"),
        ],
    )
    def test_a_public_host_is_fetched_from_its_address(self, url: str, address: str) -> None:
        assert NS["_public_address"](url) == address

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
        assert NS["_public_address"](url) is None

    @pytest.mark.parametrize(
        "url", ["ftp://manuals.example/m.pdf", "file:///etc/passwd", "https:///m.pdf"]
    )
    def test_only_http_and_https_with_a_host(self, url: str) -> None:
        assert NS["_public_address"](url) is None

    def test_a_public_literal_address_is_fetched(self) -> None:
        assert NS["_public_address"]("http://93.184.216.34/m.pdf") == "93.184.216.34"

    def test_a_host_that_does_not_resolve_is_not(self) -> None:
        assert NS["_public_address"]("https://nowhere.example/m.pdf") is None


class TestSentWhereItWasChecked:
    """The request goes to the checked address, asking for the host by name."""

    @pytest.mark.parametrize(
        ("url", "sent", "host", "tls_name"),
        [
            (
                "https://manuals.example/m.pdf?x=1",
                "https://93.184.216.34/m.pdf?x=1",
                "manuals.example",
                "manuals.example",
            ),
            (
                "https://manuals.example:8443/m.pdf",
                "https://93.184.216.34:8443/m.pdf",
                "manuals.example:8443",
                "manuals.example",
            ),
            (
                "http://cdn.example/m.pdf",
                "http://[2606:2800:220:1::1]/m.pdf",
                "cdn.example",
                None,
            ),
        ],
    )
    async def test_the_request_names_the_host_and_goes_to_its_address(
        self, url: str, sent: str, host: str, tls_name: str | None
    ) -> None:
        client = _Client({url: _Response(body=b"%PDF")})

        await NS["_fetch_public"](_Agent(), client, url)

        assert client.sent == [(sent, host, tls_name)]

    async def test_a_name_that_answers_differently_later_is_not_asked_again(self) -> None:
        # Public for the check, private for anyone who asks a second time.
        REBINDING["rebind.example"] = ["93.184.216.34", "10.0.0.1"]
        client = _Client({"https://rebind.example/m.pdf": _Response(body=b"%PDF")})

        fetched = await NS["_fetch_public"](_Agent(), client, "https://rebind.example/m.pdf")

        assert fetched is not None
        assert client.sent[0][0] == "https://93.184.216.34/m.pdf"
        REBINDING.clear()

    async def test_each_redirect_is_sent_to_its_own_checked_address(self) -> None:
        client = _Client(
            {
                "https://manuals.example/m": _Response(302, location="http://cdn.example/m.pdf"),
                "http://cdn.example/m.pdf": _Response(body=b"%PDF"),
            }
        )

        await NS["_fetch_public"](_Agent(), client, "https://manuals.example/m")

        assert [where for where, _host, _tls in client.sent] == [
            "https://93.184.216.34/m",
            "http://[2606:2800:220:1::1]/m.pdf",
        ]

    async def test_httpx_sends_the_host_and_the_tls_name_it_is_given(self) -> None:
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            fetched = await NS["_fetch_public"](_Agent(), client, "https://manuals.example/m.pdf")

        assert fetched == (200, "application/pdf", b"%PDF")
        (request,) = seen
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "manuals.example"
        assert request.extensions["sni_hostname"] == "manuals.example"


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


class TestAConnectionServesOneHost:
    """httpx pools by the address in the URL; a pinned request's connection is not kept."""

    async def test_each_pinned_request_asks_for_its_connection_to_close(self) -> None:
        client = _Client({"https://manuals.example/m.pdf": _Response(body=b"%PDF")})

        await NS["_fetch_public"](_Agent(), client, "https://manuals.example/m.pdf")

        assert client.headers[0]["Connection"] == "close"

    async def test_two_hosts_at_one_address_get_a_connection_each(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connections = 0

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal connections
            connections += 1
            try:
                while True:
                    head = await reader.readuntil(b"\r\n\r\n")
                    if b"GET /page" in head:
                        to = f"http://second.example:{port}/m.pdf".encode()
                        writer.write(b"HTTP/1.1 302 Found\r\nLocation: " + to)
                        writer.write(b"\r\nContent-Length: 0\r\n\r\n")
                    else:
                        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n%PDF")
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        # Every name is public here, and at the server's address.
        monkeypatch.setitem(NS, "_public_address", lambda _url: "127.0.0.1")
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                fetch = NS["_fetch_public"]
                first = await fetch(_Agent(), client, f"http://first.example:{port}/m.pdf")
                redirected = await fetch(_Agent(), client, f"http://first.example:{port}/page")
        finally:
            server.close()
            await server.wait_closed()

        assert first is not None and redirected is not None
        assert connections == 3


def _certificate(
    subject: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: x509.Certificate | None = None,
    issuer_key: ec.EllipticCurvePrivateKey | None = None,
) -> x509.Certificate:
    """A certificate for ``subject``: a CA's own without ``issuer``, else a server's it signs."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer.subject if issuer else name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
    )
    if issuer is None:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
    else:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(subject)]), False)
    return builder.sign(issuer_key or key, hashes.SHA256())


def _tls(tmp_path: Path, server_name: str) -> tuple[ssl.SSLContext, Path]:
    """A server context with a certificate for ``server_name``, and the file of the CA that signed it."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _certificate("test CA", ca_key)
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = _certificate(server_name, key, ca, ca_key)
    chain, private = tmp_path / "server.pem", tmp_path / "server.key"
    chain.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(chain, private)
    authority = tmp_path / "ca.pem"
    authority.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return server, authority


def _read_head(conn: socket.socket) -> bytes:
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = conn.recv(4096)
        if not chunk:
            break
        head += chunk
    return head


PDF_RESPONSE = (
    b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\nContent-Length: 4\r\n"
    b"Connection: close\r\n\r\n%PDF"
)


class _Proxy:
    """A proxy on this machine, in a thread, for one request.

    It records the heads it is sent. To CONNECT it answers ``reply`` and then
    plays the server inside the tunnel, over TLS with ``tls``; a plain request
    it answers itself, as a proxy forwarding it would.
    """

    def __init__(
        self,
        tls: ssl.SSLContext | None = None,
        reply: bytes = b"HTTP/1.1 200 Connection established\r\n\r\n",
    ) -> None:
        self._tls = tls
        self._reply = reply
        self.heads: list[bytes] = []
        self._socket = socket.create_server(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._socket.accept()
        with conn:
            conn.settimeout(30)
            head = _read_head(conn)
            self.heads.append(head)
            if not head.startswith(b"CONNECT"):
                conn.sendall(PDF_RESPONSE)
                return
            conn.sendall(self._reply)
            if not self._reply.startswith(b"HTTP/1.1 200") or self._tls is None:
                return
            try:
                with self._tls.wrap_socket(conn, server_side=True) as tunnel:
                    self.heads.append(_read_head(tunnel))
                    tunnel.sendall(PDF_RESPONSE)
            except (ssl.SSLError, OSError):
                return

    def close(self) -> None:
        self._socket.close()
        self._thread.join(timeout=30)


class _Site:
    """An HTTPS site on this machine, in a thread, for one request: it answers with a PDF."""

    def __init__(self, tls: ssl.SSLContext) -> None:
        self._tls = tls
        self.heads: list[bytes] = []
        self._socket = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._socket.accept()
        conn.settimeout(30)
        try:
            with self._tls.wrap_socket(conn, server_side=True) as site:
                self.heads.append(_read_head(site))
                site.sendall(PDF_RESPONSE)
        except (ssl.SSLError, OSError):
            conn.close()

    def close(self) -> None:
        self._socket.close()
        self._thread.join(timeout=30)


@pytest.fixture(name="proxies")
def proxies_fixture() -> Any:
    """Proxies a test starts, each closed when it ends."""
    started: list[_Proxy] = []
    yield started
    for proxy in started:
        proxy.close()


class TestThroughAProxy:
    """A proxy is asked for the checked address, so it looks nothing up; TLS inside names the host."""

    def test_an_http_proxy_leaves_https_direct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("http_proxy", "http://proxy:3128")

        assert NS["_proxy_for"]("https://manuals.example/m.pdf") is None
        assert NS["_proxy_for"]("http://manuals.example/m.pdf") == "http://proxy:3128"

    def test_a_host_in_no_proxy_goes_direct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("https_proxy", "http://proxy:3128")
        monkeypatch.setenv("no_proxy", "manuals.example")

        assert NS["_proxy_for"]("https://manuals.example/m.pdf") is None
        assert NS["_proxy_for"]("https://cdn.example/m.pdf") == "http://proxy:3128"

    def test_all_proxy_covers_every_scheme(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("all_proxy", "http://proxy:3128")

        assert NS["_proxy_for"]("https://manuals.example/m.pdf") == "http://proxy:3128"

    @pytest.mark.parametrize(
        "url", ["https://manuals.example/m.pdf", "HTTPS://manuals.example/m.pdf"]
    )
    async def test_https_is_tunnelled_to_the_checked_address_with_tls_for_the_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxies: list[_Proxy], url: str
    ) -> None:
        server, authority = _tls(tmp_path, "manuals.example")
        # The CA as an operator names a private one: the tunnel's TLS honours it.
        monkeypatch.setenv("SSL_CERT_FILE", str(authority))
        proxy = _Proxy(server)
        proxies.append(proxy)
        monkeypatch.setenv("https_proxy", proxy.url)

        async with httpx.AsyncClient(trust_env=False) as direct:
            fetched = await NS["_fetch_public"](_Agent(), direct, url)

        assert fetched == (200, "application/pdf", b"%PDF")
        connect, inside = proxy.heads
        assert connect.startswith(b"CONNECT 93.184.216.34:443 HTTP/1.1\r\n")
        assert inside.startswith(b"GET /m.pdf HTTP/1.1\r\n")
        assert b"\r\nHost: manuals.example\r\n" in inside

    async def test_a_certificate_for_another_name_is_refused_in_the_tunnel(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, proxies: list[_Proxy]
    ) -> None:
        server, authority = _tls(tmp_path, "elsewhere.example")
        monkeypatch.setenv("SSL_CERT_FILE", str(authority))
        proxy = _Proxy(server)
        proxies.append(proxy)
        monkeypatch.setenv("https_proxy", proxy.url)

        async with httpx.AsyncClient(trust_env=False) as direct:
            with pytest.raises(httpcore.ConnectError, match="Hostname mismatch"):
                await NS["_fetch_public"](_Agent(), direct, "https://manuals.example/m.pdf")

        assert len(proxy.heads) == 1

    async def test_a_proxy_that_refuses_the_tunnel_says_so(
        self, monkeypatch: pytest.MonkeyPatch, proxies: list[_Proxy]
    ) -> None:
        proxy = _Proxy(reply=b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
        proxies.append(proxy)
        monkeypatch.setenv("https_proxy", proxy.url)

        async with httpx.AsyncClient(trust_env=False) as direct:
            with pytest.raises(OSError, match=re.escape("would not open a tunnel: HTTP/1.1 407")):
                await NS["_fetch_public"](_Agent(), direct, "https://manuals.example/m.pdf")

    async def test_the_proxy_s_credentials_go_with_the_tunnel(
        self, monkeypatch: pytest.MonkeyPatch, proxies: list[_Proxy]
    ) -> None:
        proxy = _Proxy(reply=b"HTTP/1.1 403 Forbidden\r\n\r\n")
        proxies.append(proxy)
        monkeypatch.setenv("https_proxy", proxy.url.replace("//", "//ann:s%40fe@"))

        async with httpx.AsyncClient(trust_env=False) as direct:
            with pytest.raises(OSError, match="403"):
                await NS["_fetch_public"](_Agent(), direct, "https://manuals.example/m.pdf")

        # "ann:s@fe", base64
        assert b"\r\nProxy-Authorization: Basic YW5uOnNAZmU=\r\n" in proxy.heads[0]

    async def test_plain_http_is_forwarded_to_the_checked_address_naming_the_host(
        self, monkeypatch: pytest.MonkeyPatch, proxies: list[_Proxy]
    ) -> None:
        proxy = _Proxy()
        proxies.append(proxy)
        monkeypatch.setenv("http_proxy", proxy.url)

        async with httpx.AsyncClient(trust_env=False) as direct:
            fetched = await NS["_fetch_public"](_Agent(), direct, "http://manuals.example/m.pdf")

        assert fetched == (200, "application/pdf", b"%PDF")
        (head,) = proxy.heads
        assert head.startswith(b"GET http://93.184.216.34/m.pdf HTTP/1.1\r\n")
        assert b"\r\nHost: manuals.example\r\n" in head

    @pytest.mark.parametrize(
        "configured",
        [{"http_proxy": "http://proxy:3128"}, {"https_proxy": "http://p:3128", "no_proxy": "*"}],
    )
    async def test_a_request_that_goes_direct_is_pinned_though_a_proxy_is_configured(
        self, monkeypatch: pytest.MonkeyPatch, configured: dict[str, str]
    ) -> None:
        for name, value in configured.items():
            monkeypatch.setenv(name, value)
        client = _Client({"https://manuals.example/m.pdf": _Response(body=b"%PDF")})

        await NS["_fetch_public"](_Agent(), client, "https://manuals.example/m.pdf")

        assert client.sent == [
            ("https://93.184.216.34/m.pdf", "manuals.example", "manuals.example")
        ]

    async def test_the_client_itself_uses_no_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = types.ModuleType("httpx")
        fake.AsyncClient = _RecordingClient  # pyright: ignore[reportAttributeAccessIssue]
        fake.URL = httpx.URL  # pyright: ignore[reportAttributeAccessIssue]
        fake.InvalidURL = httpx.InvalidURL  # pyright: ignore[reportAttributeAccessIssue]
        fake.create_ssl_context = httpx.create_ssl_context  # pyright: ignore[reportAttributeAccessIssue]
        monkeypatch.setitem(sys.modules, "httpx", fake)
        _RecordingClient.made = []

        pdf = await NS["_download_pdf"](_Agent(), "https://manuals.example/m.pdf")

        (made,) = _RecordingClient.made
        assert pdf == b"%PDF"
        assert made.kwargs["trust_env"] is False
        # Without the environment, its CAs come in the context it is given.
        assert isinstance(made.kwargs["verify"], ssl.SSLContext)

    async def test_a_direct_download_trusts_the_ca_the_environment_names(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        server, authority = _tls(tmp_path, "manuals.example")
        monkeypatch.setenv("SSL_CERT_FILE", str(authority))
        site = _Site(server)
        # The name is public here, and at the site's address on this machine.
        monkeypatch.setitem(NS, "_public_address", lambda _url: "127.0.0.1")
        try:
            pdf = await NS["_download_pdf"](_Agent(), f"https://manuals.example:{site.port}/m.pdf")
        finally:
            site.close()

        assert pdf == b"%PDF"
        assert b"\r\nHost: manuals.example:" in site.heads[0]


class _RecordingClient(_Client):
    """`httpx.AsyncClient`, recording how it was made; every URL answers with a PDF."""

    made: ClassVar[list["_RecordingClient"]] = []

    def __init__(self, **kwargs: Any) -> None:
        super().__init__({})
        self.kwargs = kwargs
        _RecordingClient.made.append(self)

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def stream(
        self, method: str, url: str, *, headers: dict[str, str], extensions: dict[str, Any]
    ) -> _Stream:
        netloc = headers.get("Host") or urllib.parse.urlsplit(url).netloc
        self._responses.setdefault(
            urllib.parse.urlsplit(url)._replace(netloc=netloc).geturl(), _Response(body=b"%PDF")
        )
        return super().stream(method, url, headers=headers, extensions=extensions)


class TestNamesInOtherScripts:
    """A host name is looked up, and named to the server, as httpx puts it on the wire."""

    def test_it_is_looked_up_in_its_ascii_form(self) -> None:
        assert NS["_public_address"]("https://mänuals.example/m.pdf") == "93.184.216.34"

    async def test_httpx_sends_it_in_its_ascii_form(self) -> None:
        seen: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=b"%PDF")

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            fetched = await NS["_fetch_public"](_Agent(), client, "https://mänuals.example/m.pdf")

        assert fetched is not None
        (request,) = seen
        assert request.headers["host"] == "xn--mnuals-bua.example"
        assert request.extensions["sni_hostname"] == "xn--mnuals-bua.example"
