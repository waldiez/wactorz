"""Small network predicates shared by the layers that make exposure decisions.

Its own module because two unrelated places ask the same question — the MQTT
startup warning and the monitor's fail-closed bind check — and neither is a
natural home for the other's import.
"""

from __future__ import annotations

import ipaddress

#: Hostnames that mean "this machine", where traffic never leaves it.
_LOOPBACK_HOSTS = {"localhost", "localhost.localdomain", "127.0.0.1", "::1", ""}


def is_loopback(host: str) -> bool:
    """Whether traffic to `host` stays on this machine.

    Literal names first, then an address parse — `127.0.0.1` is not the only
    loopback address, and `127.0.0.53` (systemd-resolved) is one too.

    A hostname that cannot be classified without resolving it is treated as
    **not** loopback. Both callers act on the answer by warning or refusing, and
    being noisy about a host that turns out to be local costs a line, while
    staying quiet about one that is not is the failure they exist to prevent.
    """
    name = (host or "").strip().lower().strip("[]")
    if name in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False
