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

import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import aiomqtt

logger = logging.getLogger(__name__)

#: Where the install id is kept, under the state directory.
INSTALL_ID_FILE = "install_id"

#: How hard to try reading an id a racing process is still writing.
_READ_ATTEMPTS = 5
_READ_BACKOFF_SECONDS = 0.002

#: Cached for the process. The file is read once; a miss costs a stat per call
#: otherwise, and this is on the connect path of every long-lived listener.
_install_id: str | None = None


def install_id() -> str:
    """A stable identifier for this Wactorz install, minted once and kept on disk.

    MQTT client ids must be **unique per connection and stable across
    reconnects**: two connections sharing an id make the broker drop the older
    one, and a client that reconnects under a new id abandons its session. So
    every server-side id needs a component that distinguishes *this install*
    from another one on the same broker.

    Without it the ids collide. That is not hypothetical — the publisher this
    replaces pinned the literal ``wactorz-publisher``, so two Wactorz servers
    against one broker disconnected each other in a loop, for ever. A dev
    instance pointed at a shared broker was enough to trigger it.

    ⚠ **Hostname is not a substitute.** Containers get a fresh one per start, so
    it is neither stable across restarts nor meaningful as an identity.

    Created with ``O_CREAT | O_EXCL`` so only one starting process mints a
    value: the loser of the race reads the winner's rather than writing its own.
    ⚠ The file exists but is empty between the winner's ``open`` and its
    ``write``, so a loser landing in that gap would read nothing and fall back
    to its own id -- the two would then disagree for their lifetimes. The read
    is retried briefly to cover it. Those flags, and the ``FileExistsError`` the
    race raises, behave the same on Windows.

    ⚠ Written as bytes rather than text. ``os.open`` opens in text mode on
    Windows, and an ``os.fdopen(..., "w")`` on top of that translates the
    newline a second time; the reader's ``strip()`` would hide it, so the write
    is kept binary rather than left to be discovered. The ``0o600`` is a POSIX
    tidiness that Windows ignores — this is an identifier that travels in every
    client id, not a secret.
    """
    global _install_id
    if _install_id is not None:
        return _install_id

    from .paths import ensure_state_dir

    path = Path(ensure_state_dir()) / INSTALL_ID_FILE
    minted = uuid.uuid4().hex[:12]
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        minted = _read_minted(path) or minted
    except OSError:
        # An unwritable state directory leaves us with a per-process id, which
        # means durable sessions are abandoned on every restart. Say so rather
        # than degrade silently.
        logger.warning(
            "[MQTT] Could not persist an install id under %s; using a per-process "
            "one, so broker sessions will not be reused across restarts",
            path.parent,
        )
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(minted.encode("utf-8"))

    _install_id = minted
    return minted


def _read_minted(path: Path) -> str:
    """Read an id another process is creating, tolerating its empty window.

    ``O_CREAT | O_EXCL`` makes the file appear before its contents do, so a
    racing reader can arrive between the two. The wait is bounded and only ever
    happens on the first run of the first two processes, so a few milliseconds
    here costs nothing measurable and buys agreement on the identity.
    """
    for attempt in range(_READ_ATTEMPTS):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if value:
            return value
        if attempt + 1 < _READ_ATTEMPTS:
            time.sleep(_READ_BACKOFF_SECONDS)
    return ""


#: How long the broker keeps an infrastructure connection's session after it
#: disconnects. Long enough that a server restart, a node reboot or an overnight
#: outage resumes where it left off; short enough that an install that is
#: decommissioned, or a node that never comes back, stops costing broker state.
SERVER_SESSION_EXPIRY_SECONDS = 86400

#: How long the broker keeps an *agent's* session. Much shorter than the
#: server's: durability for an agent is about surviving a reconnect or a reboot,
#: not about replaying hours of readings that are stale by the time they arrive.
AGENT_SESSION_EXPIRY_SECONDS = 3600


def session_kwargs(expiry_seconds: int) -> dict[str, Any]:
    """Connect arguments that make the broker hold this session for a while.

    MQTT v5 rather than v3.1.1, which is what ``clean_session=False`` speaks.
    A v3.1.1 durable session has **no expiry**: the broker keeps it until the
    client returns with a clean session, and with ``persistence true`` -- which
    every broker this ships with sets -- it survives a broker restart too. So an
    abandoned client leaves state behind for ever, and nothing in the protocol
    reclaims it. Naming a lifetime is the whole point of moving.

    The protocol version is per connection, so callers adopt this one at a time.
    """
    import aiomqtt
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.properties import Properties

    properties = Properties(PacketTypes.CONNECT)
    properties.SessionExpiryInterval = expiry_seconds
    return {
        "protocol": aiomqtt.ProtocolVersion.V5,
        "clean_start": False,
        "properties": properties,
    }


def client_id(role: str, scope: str, detail: str | None = None) -> str:
    """Build an MQTT client id: ``wactorz-<role>-<scope>[-<detail>]``.

    ``role`` says what the connection is for (``pub``, ``srv``, ``mon``,
    ``node``, ``nodepub``), ``scope`` is what it belongs to — :func:`install_id`
    for server-side connections, the node name on a node — and ``detail``
    separates connections that share a role and scope.

    ⚠ **``detail`` is not optional for a role that opens more than one
    connection.** Main runs six long-lived listeners; giving any two of them the
    same id makes the broker kick them in a loop, inside a single process.
    """
    parts = ["wactorz", role, scope]
    if detail:
        parts.append(detail)
    return "-".join(parts)


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
