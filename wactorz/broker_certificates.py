# SPDX-License-Identifier: Apache-2.0
"""Issue the MQTT broker's TLS certificate: ``python -m wactorz.broker_certificates``.

Run before a broker Wactorz provides starts -- by the add-on's ``run.sh`` and by
docker compose -- so the broker finds a certificate issued by this install's CA,
naming every address a node may reach it by. It mints the CA the first time, and
issues the broker certificate again only when the one there will not do; see
:mod:`wactorz.core.broker_tls`.

``--export`` copies the certificate and key somewhere a broker reads them, under
the names that broker is configured with.

The server does the same when it starts, through
:func:`wactorz.broker_files.prepare_broker_files`. This module holds the command
alone and is imported by no other, so ``python -m`` finds it not yet loaded
(``tests/test_package_entry_points.py`` checks that).
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from .broker_files import account_nodes, broker_names, full_access_accounts, node_data_topics
from .config import CONFIG
from .core import broker_accounts, broker_tls

logger: logging.Logger = logging.getLogger(__name__)


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
            _export_accounts(args.export)
    if args.logins is not None:
        nodes = account_nodes()
        args.logins.parent.mkdir(parents=True, exist_ok=True)
        args.logins.write_text(broker_accounts.home_assistant_logins(nodes), encoding="utf-8")
        # Password hashes, for whoever administers the broker and nobody else
        # with an account on the machine.
        args.logins.chmod(0o600)
    return 0


def _export_accounts(directory: Path) -> None:
    """Write the node accounts beside an exported certificate, when they can be written.

    A setting that cannot be used -- a data topic that would open what the
    access list exists to close -- costs the accounts and is said, and nothing
    else: the certificate beside them is already there, and whoever called this
    reads a failure as there being no certificate to serve.
    """
    try:
        broker_accounts.write_files(
            directory, account_nodes(), full_access_accounts(), node_data_topics()
        )
    except ValueError as exc:
        logger.warning("[mqtt-accounts] The node accounts were not written: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
