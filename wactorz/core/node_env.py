"""What a node is told about itself: the settings in its ``.env``.

A node learns its name, its broker, its signing key and its broker account from
one file, written by whoever made it: ``/deploy`` over SSH, or, for the sandbox
node in the compose stack, a one-shot container (see
:mod:`wactorz.node.compose_env`). Both write it from here, so a node made either
way is told the same things in the same form.

The file is read by two parsers: a shell sources it on the ``nohup`` path, and
systemd reads it as an ``EnvironmentFile`` under a unit. ``shlex.quote`` is the
safe intersection -- systemd accepts single-quoted values and performs no
command substitution -- so a value that is safe to source is also taken
literally by systemd.
"""

import shlex

from ..config import CONFIG, NODE_SIGNING
from . import broker_accounts
from .node_signing import next_sequence, node_key


class UnusableNodeAccountError(ValueError):
    """A node's name cannot be the broker account it would be deployed with."""

    def __init__(self, problem: str) -> None:
        super().__init__(problem)


def node_account(node: str, user: str = "", password: str = "") -> tuple[str, str]:
    """The broker account a node presents: one of its own, a derived one, or the server's.

    A derived account only where the broker has one -- the brokers Wactorz
    configures, with `WACTORZ_NODE_ACCOUNTS` set -- because an account no broker
    knows leaves the node authenticating against nothing. Anywhere else the
    server's own account stays the default, as it has been. ``user`` and
    ``password``, when set for the node, win over both.
    """
    default_user, default_password = CONFIG.mqtt_username or "", CONFIG.mqtt_password or ""
    if CONFIG.node_accounts:
        problem = broker_accounts.name_error(node)
        if problem:
            raise UnusableNodeAccountError(problem)
        default_user = node
        default_password = broker_accounts.password(node)
    return user or default_user, password or default_password


def node_env_text(
    node: str,
    broker: str,
    port: int,
    *,
    username: str,
    password: str,
    tls_ca: str | None = None,
    check_hostname: bool = False,
    extensions: str = "",
) -> str:
    """The node's ``.env``: who it is, where its broker is, and how it proves both.

    ``tls_ca`` is the CA's path as the node reads it, or None for plain MQTT.
    The signing key goes with the sequence number main has reached and what to
    do with a control message not signed for it (``WACTORZ_NODE_SIGNING``); see
    :mod:`wactorz.core.node_signing`. ``extensions`` is the node's
    ``WACTORZ_EXTENSIONS``; see :mod:`wactorz.installed`.
    """
    lines = [
        f"WACTORZ_NODE={shlex.quote(node)}",
        f"WACTORZ_BROKER={shlex.quote(broker)}",
        f"WACTORZ_PORT={shlex.quote(str(port))}",
        f"WACTORZ_NODE_KEY={shlex.quote(node_key(node))}",
        f"WACTORZ_CONTROL_SINCE={next_sequence()}",
        f"WACTORZ_NODE_SIGNING={shlex.quote(NODE_SIGNING)}",
    ]
    if extensions:
        lines.append(f"WACTORZ_EXTENSIONS={shlex.quote(extensions)}")
    if tls_ca is not None:
        lines.append("MQTT_TLS=1")
        lines.append(f"MQTT_TLS_CA={shlex.quote(tls_ca)}")
        lines.append(f"MQTT_TLS_CHECK_HOSTNAME={1 if check_hostname else 0}")
    if username or password:
        lines.append(f"MQTT_USERNAME={shlex.quote(username)}")
        lines.append(f"MQTT_PASSWORD={shlex.quote(password)}")
    return "\n".join(lines) + "\n"
