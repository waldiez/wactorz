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
