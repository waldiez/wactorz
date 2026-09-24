"""Which callers a browser may act on this server's behalf for.

Two separate questions, both answered here because they fail together:

* **Origin** — who is asking. A browser attaches it to cross-origin requests and
  to same-origin state-changing ones, so a mismatch identifies a page acting on
  a user's session from somewhere else.
* **Host** — which name the request arrived under. Origin alone cannot see a DNS
  rebinding attack: the attacker's page and the address it resolves to are the
  same name, so `Origin` and `Host` agree and the comparison passes while the
  request reaches a loopback server. Pinning the acceptable names is what closes
  that, and it is why a hostname nobody configured is refused even when it
  resolves here.

Non-browser callers send neither header and are unaffected; scripts, `curl` and
the Python client keep working.

Both checks read the name and scheme through `X-Forwarded-*` only when the peer
is a proxy listed in `WACTORZ_TRUSTED_PROXIES`. From anyone else those headers
are ignored: a rebound page can send them on a same-origin request with no
preflight, and believing `X-Forwarded-Host: localhost` from it is the rebinding
attack the host check exists to stop.
"""

import ipaddress
import logging
from typing import Any

from aiohttp import web

from .. import config
from .static_site import ingress_path_of

logger = logging.getLogger(__name__)

#: Ingress peers already named in the log, so each is reported once.
_seen_peers: set[Any] = set()

#: Peers already told their forwarded headers were ignored, so each is reported
#: once — up to a cap, since these addresses are the caller's to choose.
_ignored_peers: set[Any] = set()
_IGNORED_PEERS_CAP = 256

#: Scheme defaults that never appear in an Origin header.
_DEFAULT_PORTS = {"http": "80", "https": "443"}

#: A WebSocket origin is written with the page's scheme, not the socket's, but
#: normalising both ways means a caller listed as `https://x` also matches a
#: `wss://x` handshake.
_SCHEME_ALIASES = {"ws": "http", "wss": "https"}

#: Names that always mean "this machine", so a loopback install works untouched.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", ""}

#: The loopback networks by IP version, for spotting a trusted-proxy entry that
#: would also trust the user's own browser.
_LOOPBACK_NETS = {4: ipaddress.ip_network("127.0.0.0/8"), 6: ipaddress.ip_network("::1/128")}


def normalize_origin(value: str) -> str:
    """`scheme://host[:port]`, lowercased, default port dropped, ws mapped to http.

    Returns "" for anything unparseable, including the literal `null` a
    sandboxed iframe or a `file://` page sends — those are deliberately not
    treated as same-origin, because nothing identifies who they are.
    """
    raw = (value or "").strip()
    if not raw or raw == "null" or "://" not in raw:
        return ""
    scheme, _, rest = raw.partition("://")
    scheme = _SCHEME_ALIASES.get(scheme.lower(), scheme.lower())
    host = rest.split("/", 1)[0].lower()
    if not host:
        return ""
    name, port = _split_host_port(host)
    if port and port == _DEFAULT_PORTS.get(scheme):
        port = ""
    return f"{scheme}://{name}:{port}" if port else f"{scheme}://{name}"


def _split_host_port(host: str) -> tuple[str, str]:
    """Split `host[:port]`, keeping an IPv6 literal's brackets intact."""
    if host.startswith("["):
        closing = host.find("]")
        if closing == -1:
            return host, ""
        name = host[: closing + 1]
        rest = host[closing + 1 :]
        return name, rest[1:] if rest.startswith(":") else ""
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        return name, port
    return host, ""


def _parse_networks(
    raw: str, setting: str
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Comma-separated addresses or CIDRs, skipping (and naming) malformed ones."""
    nets = []
    for part in raw.split(","):
        entry = part.strip()
        if not entry:
            continue
        try:
            nets.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("[origin] ignoring malformed %s entry %r", setting, entry)
    return tuple(nets)


def _peer_address(request: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The connecting address, or None when there is none to read."""
    try:
        return ipaddress.ip_address(getattr(request, "remote", None) or "")
    except ValueError:
        return None


def from_trusted_proxy(request: Any) -> bool:
    """Whether the connecting peer is a reverse proxy named in `WACTORZ_TRUSTED_PROXIES`."""
    address = _peer_address(request)
    if address is None:
        return False
    nets = _parse_networks(config.TRUSTED_PROXIES, "WACTORZ_TRUSTED_PROXIES")
    return any(address in net for net in nets)


def _note_ignored(request: Any, header: str) -> None:
    """Say once per peer that its forwarded headers were not believed.

    Otherwise a real proxy nobody listed looks like a server refusing its own
    name. Bounded, because the addresses belong to whoever is calling.
    """
    peer = getattr(request, "remote", None)
    if peer in _ignored_peers or len(_ignored_peers) >= _IGNORED_PEERS_CAP:
        return
    _ignored_peers.add(peer)
    logger.warning(
        "[origin] ignoring %s from %s — add it to WACTORZ_TRUSTED_PROXIES if it is your proxy",
        header,
        peer,
    )


def _forwarded(request: Any, header: str) -> str:
    """The first hop of a forwarded header, or "" when this peer may not set it.

    The first hop is what the browser addressed. That holds only if the trusted
    proxy sets the header rather than appending to one the client sent — which
    is what `proxy_set_header` does, and what the deployment docs ask for.
    """
    value = request.headers.get(header, "")
    if not value:
        return ""
    if not from_trusted_proxy(request):
        _note_ignored(request, header)
        return ""
    return value.split(",")[0].strip()


def request_host(request: Any) -> str:
    """The `host[:port]` the browser addressed, seen through a trusted proxy."""
    return _forwarded(request, "X-Forwarded-Host") or request.headers.get("Host", "")


def request_scheme(request: Any) -> str:
    """The scheme the browser used, seen through a trusted proxy.

    Behind a proxy that terminates TLS the direct scheme is always `http`, which
    is why this, not `request.scheme`, decides whether a cookie is `Secure`.
    """
    return (_forwarded(request, "X-Forwarded-Proto") or request.scheme).lower()


def client_address(request: Any) -> str:
    """The client's address, seen through any trusted proxies.

    `X-Forwarded-For` is read from the right, because each proxy appends the
    peer it saw: the first hop that is not itself a trusted proxy is the
    client. Everything to its left is whatever the client chose to send, so it
    is never taken while an untrusted hop stands to its right.
    """
    peer = getattr(request, "remote", None) or ""
    if not from_trusted_proxy(request):
        return peer
    hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
    nets = _parse_networks(config.TRUSTED_PROXIES, "WACTORZ_TRUSTED_PROXIES")
    for hop in reversed(hops):
        try:
            address = ipaddress.ip_address(hop)
        except ValueError:
            return hop
        if not any(address in net for net in nets):
            return hop
    return hops[0] if hops else peer


def request_origin(request: Any) -> str:
    """The origin this request was addressed to.

    Built from the same host and scheme the host check reads, so the two can
    never be answered from different sources. Behind a trusted proxy that is
    the name the browser used; comparing a browser's Origin against the proxy's
    internal one would reject every legitimate call.
    """
    host = request_host(request)
    if not host:
        return normalize_origin(str(request.url.origin()))
    return normalize_origin(f"{request_scheme(request)}://{host}")


def parse_allow_list(raw: str) -> set[str]:
    """Normalise a comma-separated origin allow-list."""
    return {o for o in (normalize_origin(part) for part in (raw or "").split(",")) if o}


def origin_allowed(origin: str, request: Any, allowed: set[str]) -> bool:
    """Whether `origin` may act on this server.

    An absent header means a non-browser caller and is decided by the caller of
    this function, not here — this answers only "is this origin one of ours".
    """
    candidate = normalize_origin(origin)
    if not candidate:
        return False
    return candidate == request_origin(request) or candidate in allowed


def host_allowed(request: Any, allowed_hosts: set[str]) -> bool:
    """Whether the name this request arrived under is one we answer to.

    A rebinding attack needs a *hostname* it controls, so an IP literal cannot
    be one and is always accepted — which keeps reaching the dashboard at
    `http://192.168.1.2:8888` working. A name that is neither loopback nor
    configured is refused even though it resolved here, because that is exactly
    what rebinding looks like.
    """
    name, _ = _split_host_port(request_host(request).strip().lower())
    if name in _LOOPBACK_NAMES or name in allowed_hosts:
        return True
    bare = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        return False
    return True


def parse_host_list(raw: str) -> set[str]:
    """Normalise a comma-separated host allow-list (names only, no ports)."""
    out: set[str] = set()
    for part in (raw or "").split(","):
        name, _ = _split_host_port(part.strip().lower())
        if name:
            out.add(name)
    return out


# Methods that change something. A cross-origin *read* is already prevented by
# withholding the Access-Control-Allow-Origin header — the browser refuses to
# hand the response to the page — so a refusal is only owed where the request
# would have had an effect before anyone read the reply.
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _trusted_peers() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """The networks the ingress bypass is accepted from."""
    return _parse_networks(config.INGRESS_PEERS, "WACTORZ_INGRESS_PEERS")


def log_mode() -> None:
    """State once, at startup, whether the ingress bypass exists here.

    Otherwise "off" and "on but never needed" look identical from the outside:
    both are simply an absence of lines, and telling them apart means reading
    `run.sh` on the host. Reported at warning when the bypass is available, since
    it is the one path that skips the origin and host checks and an operator
    should see it on a deployment that logs only warnings.
    """
    if not config.INGRESS_ENABLED:
        logger.info("[origin] ingress mode off — the bypass is unavailable")
        return
    peers = ", ".join(str(net) for net in _trusted_peers()) or "nothing"
    logger.warning(
        "[origin] ingress mode on — requests from %s may skip the origin and host checks",
        peers,
    )


def warn_loopback_proxies() -> None:
    """Warn at startup when `WACTORZ_TRUSTED_PROXIES` covers a loopback address.

    A browser on this machine connects from loopback too, so a rebound page that
    reaches the server directly is then believed when it names `localhost` in
    `X-Forwarded-Host`. That is sometimes the price of a proxy on the same host,
    and it should be paid knowingly: having the proxy pass `Host` through avoids
    it for every check except the cookie's `Secure` flag.
    """
    nets = _parse_networks(config.TRUSTED_PROXIES, "WACTORZ_TRUSTED_PROXIES")
    loopback = [str(net) for net in nets if net.overlaps(_LOOPBACK_NETS[net.version])]
    if loopback:
        logger.warning(
            "[origin] WACTORZ_TRUSTED_PROXIES includes loopback (%s): a web page open in a "
            "browser on this machine can claim any host name, which undoes the DNS "
            "rebinding check. Prefer having a local proxy pass Host through.",
            ", ".join(loopback),
        )


def from_supervisor(request: Any) -> bool:
    """Whether this request came from Home Assistant's ingress proxy itself.

    Three things have to agree, because each alone is forgeable or coincidental.
    `ingress_path_of` covers the first two — the deployment declares that it sits
    behind ingress, and the marker is a plain URL path. This adds the third.

    The marker cannot decide it alone: any peer on the container network can set
    one, and since the add-on publishes no ports this bypass is the only way past
    the origin and host checks. So it is corroborated by where the request came
    from, which a peer on another host cannot forge.

    A peer we cannot see (a unix socket, an unusual transport) is not trusted:
    absence of evidence is not evidence.
    """
    if not ingress_path_of(request):
        return False
    peer = getattr(request, "remote", None)
    try:
        address = ipaddress.ip_address(peer or "")
    except ValueError:
        logger.warning("[origin] ingress header from an unreadable peer %r — refused", peer)
        return False
    if any(address in net for net in _trusted_peers()):
        if address not in _seen_peers:
            # Named once per address so an operator can confirm which proxy is
            # actually reaching them, rather than trusting a documented range.
            # At warning because it is the fact someone goes looking for, and a
            # deployment that logs only warnings is exactly where it is needed.
            _seen_peers.add(address)
            logger.warning("[origin] accepting the ingress bypass from %s", address)
        return True
    logger.warning(
        "[origin] ingress header from %s, which is outside WACTORZ_INGRESS_PEERS — refused",
        address,
    )
    return False


def _allow_lists() -> tuple[set[str], set[str]]:
    """Read the settings at call time, so a reconfigured process is not stale."""
    return parse_allow_list(config.CORS_ORIGINS), parse_host_list(config.ALLOWED_HOSTS)


def refuse(request: Any, *, strict_origin: bool = False) -> web.Response | None:
    """A 403 response when this request may not act here, otherwise None.

    `strict_origin` refuses a mismatched origin on *any* method, which is what a
    WebSocket handshake needs: it is a GET, and withholding a response header
    protects nothing once the socket is open — the page can already read the
    live feed and send commands.

    Logged at warning with the offending name, because a refusal a user did not
    expect is otherwise indistinguishable from the server being broken, and the
    value in the message is exactly what has to be added to
    `WACTORZ_ALLOWED_HOSTS` or `WACTORZ_CORS_ORIGINS` to fix it.
    """
    if from_supervisor(request):
        # Behind Home Assistant's Supervisor, whose own login is the boundary.
        # Which internal name and scheme it forwards under is its business, and
        # depending on that would refuse the whole panel.
        #
        # A browser cannot forge this: a custom request header triggers a
        # preflight, and it is deliberately absent from the Allow-Headers below,
        # so the browser refuses before sending the real request. Nor can another
        # peer on the container network, whatever headers it sets, because the
        # marker is only honoured from Supervisor's own address range.
        #
        # Both halves are required: the value is validated rather than merely
        # present, and it is corroborated by the peer address, so neither a
        # garbage header nor a header from the wrong host buys a bypass.
        return None

    allowed_origins, allowed_hosts = _allow_lists()

    if not host_allowed(request, allowed_hosts):
        logger.warning(
            "[origin] refused host %r — add it to WACTORZ_ALLOWED_HOSTS if this is yours",
            request_host(request),
        )
        return web.json_response({"error": "host not allowed"}, status=403)

    origin = request.headers.get("Origin", "")
    if not origin:
        return None  # not a browser; nothing claims to be acting on anyone's behalf
    if origin_allowed(origin, request, allowed_origins):
        return None
    if strict_origin or request.method in STATE_CHANGING:
        logger.warning(
            "[origin] refused %r on %s %s — add it to WACTORZ_CORS_ORIGINS if this is yours",
            origin,
            request.method,
            request.path,
        )
        return web.json_response({"error": "origin not allowed"}, status=403)
    return None


def allowed_origin(request: Any) -> str:
    """The origin to name in the response headers, or "" to name none."""
    origin = request.headers.get("Origin", "")
    allowed_origins, _ = _allow_lists()
    return origin if origin and origin_allowed(origin, request, allowed_origins) else ""


def cors_headers(origin: str) -> dict[str, str]:
    """Headers granting `origin` access, or only `Vary` when there is none.

    `Vary: Origin` is sent either way: the answer depends on the request's
    Origin, so a cache that ignored it would serve one caller's grant to
    another.
    """
    if not origin:
        return {"Vary": "Origin"}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Vary": "Origin",
    }
