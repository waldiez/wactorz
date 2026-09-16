"""Issue the MQTT broker's TLS certificate: ``python -m wactorz.broker_certificates``.

Run before a broker Wactorz provides starts -- by the add-on's ``run.sh`` and by
docker compose -- so the broker finds a certificate issued by this install's CA,
naming every address a node may reach it by. It mints the CA the first time, and
issues the broker certificate again only when the one there will not do; see
:mod:`wactorz.core.broker_tls`.

``--export`` copies the certificate and key somewhere a broker reads them, under
the names that broker is configured with.

:func:`prepare_server_tls` does the same when the server starts with ``MQTT_TLS`` on.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import CONFIG
from .core import broker_accounts, broker_tls
from .core.mqtt_tls import SYSTEM_TRUST, client_context, generated_ca_path, tls_enabled

logger = logging.getLogger(__name__)


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
    nodes = [target.name for target in CONFIG.deploy_targets]
    try:
        changed = broker_accounts.write_files(directory, nodes)
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


def main(argv: Sequence[str] | None = None) -> int:
    """Issue what is missing, export it if asked, and say what was done."""
    parser = argparse.ArgumentParser(
        prog="python -m wactorz.broker_certificates",
        description="Issue the MQTT broker's TLS certificate from this install's CA.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=None,
        help="Where the CA and broker certificate are kept (default: <state>/mqtt_tls).",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="Another address the broker is reached by. May be given more than once.",
    )
    parser.add_argument(
        "--export", type=Path, default=None, help="Copy the files into this directory."
    )
    parser.add_argument("--cert-name", default=broker_tls.BROKER_CERT_FILE)
    parser.add_argument("--key-name", default=broker_tls.BROKER_KEY_FILE)
    parser.add_argument("--ca-name", default="", help="Also export the CA under this name.")
    parser.add_argument(
        "--logins",
        type=Path,
        default=None,
        help="Write the node accounts as a logins: block for the official Mosquitto add-on.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    files = broker_tls.ensure([*broker_names(), *args.name], directory=args.dir)
    if args.export is not None:
        broker_tls.export(
            files,
            args.export,
            cert_name=args.cert_name,
            key_name=args.key_name,
            ca_name=args.ca_name,
        )
        # The same folder carries the accounts, so one call leaves a broker of ours
        # everything it reads.
        if CONFIG.node_accounts:
            broker_accounts.write_files(
                args.export, [target.name for target in CONFIG.deploy_targets]
            )
    if args.logins is not None:
        nodes = [target.name for target in CONFIG.deploy_targets]
        args.logins.parent.mkdir(parents=True, exist_ok=True)
        args.logins.write_text(broker_accounts.home_assistant_logins(nodes), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
