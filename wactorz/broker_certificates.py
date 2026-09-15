"""Issue the MQTT broker's TLS certificate: ``python -m wactorz.broker_certificates``.

Run before a broker Wactorz provides starts -- by the add-on's ``run.sh`` and by
docker compose -- so the broker finds a certificate issued by this install's CA,
naming every address a node may reach it by. It mints the CA the first time, and
issues the broker certificate again only when the one there will not do; see
:mod:`wactorz.core.broker_tls`.

``--export`` copies the certificate and key somewhere a broker reads them, under
the names that broker is configured with.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import CONFIG
from .core import broker_tls


def broker_names() -> list[str]:
    """Every address this configuration says the broker is reached by.

    The broker this server connects to, this machine's name, and the broker
    address each deploy target gives its node.
    """
    names = [CONFIG.mqtt_host, socket.gethostname()]
    names.extend(target.broker for target in CONFIG.deploy_targets if target.broker)
    return names


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
