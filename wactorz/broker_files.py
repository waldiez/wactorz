# SPDX-License-Identifier: Apache-2.0
"""What a broker Wactorz provides reads, written when the server starts.

:func:`prepare_broker_files` makes this server's broker TLS ready, and writes the
node accounts and their access list beside the certificate when those are on;
see :mod:`wactorz.core.broker_tls` and :mod:`wactorz.core.broker_accounts`. The
same files are issued before such a broker starts by
``python -m wactorz.broker_certificates``, which reads them from here.

Kept apart from that command so the server can import this without loading the
module ``python -m`` runs.
"""

import logging
import socket
from pathlib import Path

from .config import CONFIG, MODEL_CODE_NODE, RESERVED_NODE_NAMES
from .core import broker_accounts, broker_tls
from .core.mqtt_tls import SYSTEM_TRUST, client_context, generated_ca_path, tls_enabled

logger: logging.Logger = logging.getLogger(__name__)


def broker_names() -> list[str]:
    """Every address this configuration says the broker is reached by.

    The broker this server connects to, this machine's name, and the broker
    address each deploy target gives its node.
    """
    names = [CONFIG.mqtt_host, socket.gethostname()]
    names.extend(target.broker for target in CONFIG.deploy_targets if target.broker)
    return names


def prepare_broker_files() -> str:
    """Write what a broker of ours reads, and check this server's own TLS.

    Returns why the server cannot start, or ``""``. The accounts are written first
    and never stop a start: a broker that has not read them yet refuses a node, not
    this server.
    """
    _write_node_accounts()
    return _prepare_server_tls()


def _write_node_accounts() -> None:
    """Write the node accounts and access list, when they are on and there is somewhere to.

    The nodes are the deploy targets: adding one is a configuration change, which
    means a restart, which is when this runs. A broker reads these files when it
    starts or when it is told to read them again, so a change is worth one line in
    the log.
    """
    if not (CONFIG.node_accounts and CONFIG.mqtt_broker_dir):
        return
    directory = Path(CONFIG.mqtt_broker_dir).expanduser()
    nodes = account_nodes()
    try:
        full_access = full_access_accounts()
        changed = broker_accounts.write_files(directory, nodes, full_access, node_data_topics())
    except (OSError, ValueError) as exc:
        logger.warning(
            "[mqtt-accounts] Could not write the node accounts to %s: %s", directory, exc
        )
        return
    if changed:
        logger.warning(
            "[mqtt-accounts] Wrote %d node account(s) and the access list to %s. A broker reads "
            "them when it starts, and a reload does not pick up an access list it did not already "
            "have: restart it before deploying those nodes. The compose broker does that itself.",
            len(nodes),
            directory,
        )
        if nodes:
            logger.warning(
                "[mqtt-accounts] On that broker, %s keep%s every topic, each node has its own "
                "and the shared agent and data topics, and any other account has none. List "
                "another system's account in WACTORZ_BROKER_ACCOUNTS, and a data topic a node's "
                "agents use in WACTORZ_NODE_TOPICS.",
                " and ".join(full_access) if full_access else "clients with no account",
                "s" if len(full_access) == 1 else "",
            )


def account_nodes() -> list[str]:
    """The nodes that can have a broker account, having named those that cannot.

    The deploy targets, and the node model-written code is sent to
    (``WACTORZ_MODEL_CODE_NODE``), which in the compose stack is a container
    rather than a machine reached over SSH.

    A node's name is its account's name, and a broker does not take every name:
    one with a space in it, say. That node is left out and said so, rather than
    refusing them all: the others still get their accounts, and `/deploy`
    refuses the one that has none, with the same reason.
    """
    usable = []
    for name in [*(target.name for target in CONFIG.deploy_targets), MODEL_CODE_NODE]:
        # `main` as the model-code node means "keep the code on main": no node.
        if not name or name in usable or name.lower() in RESERVED_NODE_NAMES:
            continue
        problem = broker_accounts.name_error(name)
        if problem:
            logger.warning("[mqtt-accounts] %s Until then it has no broker account.", problem)
        else:
            usable.append(name)
    return usable


def node_is_under_access_list(node: str) -> bool:
    """Whether this server wrote an access list that holds ``node`` to its own topics.

    True for a node with an account on a broker Wactorz writes the files for. On
    any other broker the rules, if there are any, are not this server's to know.
    """
    if not (CONFIG.node_accounts and CONFIG.mqtt_broker_dir):
        return False
    named = {target.name for target in CONFIG.deploy_targets} | {MODEL_CODE_NODE}
    return (
        bool(node)
        and node in named
        and node.lower() not in RESERVED_NODE_NAMES
        and broker_accounts.name_error(node) is None
    )


def full_access_accounts() -> list[str]:
    """The accounts the access list gives the whole broker: this server's, and those listed."""
    listed = broker_accounts.parse_accounts(CONFIG.broker_accounts)
    own = [CONFIG.mqtt_username] if CONFIG.mqtt_username else []
    return sorted({*own, *listed})


def node_data_topics() -> list[tuple[str, str]]:
    """The data topics a node may use: the conventional ones, and those the setting adds."""
    return [*broker_accounts.NODE_TOPICS, *broker_accounts.parse_node_topics(CONFIG.node_topics)]


def _prepare_server_tls() -> str:
    """Make this server's broker TLS ready at startup; why it cannot be, or ``""``.

    Nothing to do with ``MQTT_TLS`` off. With the CA Wactorz generates
    (``MQTT_TLS_CA`` blank), the CA and broker certificate are created when missing,
    and the broker's copy is written to ``MQTT_BROKER_DIR`` -- for a broker beside
    this server that reads it from there, as the compose stacks' does. A CA of your
    own is only checked, never generated.

    The CA has to load either way. Connecting unverified is not an option, and
    failing every connection, for ever, with a missing-file error that names no file
    is no better than refusing to start.
    """
    if not tls_enabled(CONFIG.mqtt_tls):
        return ""
    ca = CONFIG.mqtt_tls_ca.strip()
    if not ca:
        try:
            files = broker_tls.ensure(broker_names())
        except (OSError, ValueError) as exc:
            return f"MQTT_TLS is on, but the broker's TLS certificate could not be issued: {exc}"
        if CONFIG.mqtt_broker_dir:
            _write_for_broker(files, Path(CONFIG.mqtt_broker_dir).expanduser())
    try:
        client_context(ca, CONFIG.mqtt_tls_check_hostname)
    except OSError as exc:
        if ca.lower() == SYSTEM_TRUST:
            where = "the system trust store"
        else:
            where = str(Path(ca).expanduser() if ca else generated_ca_path())
        return (
            f"MQTT_TLS is on, but the CA to verify the broker with could not be loaded from "
            f"{where} ({exc}). Set MQTT_TLS_CA to your CA's file, leave it blank for the one "
            "Wactorz generates, or set MQTT_TLS=0."
        )
    return ""


def _write_for_broker(files: broker_tls.BrokerFiles, directory: Path) -> None:
    """Write the broker's certificate and key to ``directory``, when they changed.

    Unchanged files are left alone, so a restart of this server does not tell anyone
    to restart the broker for nothing. Not fatal when it fails: the broker may get its
    certificate some other way, and this server's own connection does not need it.
    """
    chain = files.cert.read_bytes() + files.ca.read_bytes()
    key = files.key.read_bytes()
    cert_path = directory / broker_tls.BROKER_CERT_FILE
    key_path = directory / broker_tls.BROKER_KEY_FILE
    if _holds(cert_path, chain) and _holds(key_path, key):
        return
    try:
        broker_tls.export(
            files,
            directory,
            cert_name=broker_tls.BROKER_CERT_FILE,
            key_name=broker_tls.BROKER_KEY_FILE,
        )
    except OSError as exc:
        logger.warning(
            "[mqtt-tls] Could not write the broker's TLS certificate to %s: %s", directory, exc
        )
        return
    logger.warning(
        "[mqtt-tls] Wrote the broker's TLS certificate to %s. A broker reads it only when it "
        "starts: restart it to serve TLS (docker compose restart mosquitto).",
        directory,
    )


def _holds(path: Path, data: bytes) -> bool:
    try:
        return path.read_bytes() == data
    except OSError:
        return False
