"""Central aiomqtt client factory.

All in-process MQTT connections go through :func:`mqtt_client` so broker
credentials (``MQTT_USERNAME`` / ``MQTT_PASSWORD``) are applied consistently.

Without this, an external broker with ``allow_anonymous false`` — e.g. the
official Home Assistant Mosquitto add-on — rejects every connection, which is
why the add-on historically only worked with its bundled, anonymous broker.

Both ``aiomqtt`` and ``CONFIG`` are imported lazily inside the factory, so this
module has **zero import-time side effects**. That matters because
``core/actor.py`` imports this at the top and is itself imported very early by
``wactorz/__init__.py`` — a module-level ``from ..config import CONFIG`` here
re-enters the half-initialised package and causes a circular import.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import aiomqtt


def mqtt_client(hostname: str, port: int, **kwargs: Any) -> aiomqtt.Client:
    """Build an ``aiomqtt.Client`` with broker credentials injected from CONFIG.

    Credentials are only added when configured *and* not already supplied by
    the caller, so explicit per-call overrides still win.
    """
    import aiomqtt

    from ..config import CONFIG

    if "username" not in kwargs and CONFIG.mqtt_username:
        kwargs["username"] = CONFIG.mqtt_username
    if "password" not in kwargs and CONFIG.mqtt_password:
        kwargs["password"] = CONFIG.mqtt_password
    return aiomqtt.Client(hostname, port, **kwargs)


#: Hostnames that mean "this machine", where broker traffic never leaves it.
_LOOPBACK_HOSTS = {"localhost", "localhost.localdomain", "127.0.0.1", "::1", ""}


def _is_loopback(host: str) -> bool:
    """Whether traffic to `host` stays on this machine.

    Literal names first, then an address parse — `127.0.0.1` is not the only
    loopback address, and `127.0.0.53` (systemd-resolved) is one too.
    """
    name = (host or "").strip().lower().strip("[]")
    if name in _LOOPBACK_HOSTS:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        # A hostname we cannot classify without resolving it. Treated as remote:
        # warning about a broker that turns out to be local is a wasted line,
        # while staying quiet about one that is not is the failure this exists
        # to prevent.
        return False


def broker_exposure_warning(host: str, username: str) -> str | None:
    """What to say at startup about a broker that is not on this machine.

    ⚠ Naming what actually travels, because the risk is not "unencrypted
    telemetry". The runner **executes code delivered over the broker**
    (`nodes/<name>/spawn`), so anyone who can read the wire sees that code and
    anyone who can write to it runs code on every node. Plaintext plus anonymous
    is remote code execution offered to the network segment.

    There is no TLS support in this client, so a non-loopback broker is always
    plaintext — the message says so rather than implying a setting exists to
    turn it on. Returns None when the broker is local, where none of this bites.
    """
    if _is_loopback(host):
        return None
    anonymous = not (username or "").strip()
    return (
        f"MQTT broker {host} is not on this machine and the connection is unencrypted"
        f"{' and unauthenticated' if anonymous else ''}. "
        "Broker traffic includes the code spawned agents run, so anyone who can "
        "read this network sees it"
        f"{' and anyone who can reach the broker can run code on every node' if anonymous else ''}. "
        "Keep the broker on localhost, or put it on a network you trust"
        f"{' and set MQTT_USERNAME/MQTT_PASSWORD' if anonymous else ''}."
    )
