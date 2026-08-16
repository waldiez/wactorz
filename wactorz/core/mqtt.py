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


def broker_exposure_warning(host: str, username: str) -> str | None:
    """What to say at startup about a broker that is not on this machine.

    Naming what actually travels, because the risk is not "unencrypted
    telemetry". The runner **executes code delivered over the broker**
    (`nodes/<name>/spawn`), so anyone who can read the wire sees that code and
    anyone who can write to it runs code on every node. Plaintext plus anonymous
    is remote code execution offered to the network segment.

    There is no TLS support in this client, so a non-loopback broker is always
    plaintext — the message says so rather than implying a setting exists to
    turn it on. Returns None when the broker is local, where none of this bites.
    """
    from .net import is_loopback

    if is_loopback(host):
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
