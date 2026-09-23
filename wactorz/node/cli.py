"""Starting a node: ``wactorz-node --node <name>``, or ``wactorz --node <name>``.

The node half of :mod:`wactorz.cli`. Two checks happen before anything connects,
because both describe a node that can never work and both would otherwise show
up as a runner that reconnects every three seconds for ever:

- a node name that cannot be an MQTT topic level, which the broker refuses on
  every publish and every subscribe;
- TLS turned on with a CA that cannot be loaded, which is not going to appear.

Both exit 2 — the status the systemd unit is told not to restart on.
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import uuid

from ..config import CONFIG, deploy_name_error
from ..core.mqtt_tls import client_context, tls_enabled
from .runner import NodeRunner

logger = logging.getLogger(__name__)


def node_name_from(args: argparse.Namespace) -> str:
    """The name this node answers to.

    ``--node`` is the flag; ``--name`` is what the single-file runner took, and
    still works so an existing unit file or launcher does not have to change on
    the day the package arrives. ``WACTORZ_NODE`` comes last and matters more
    than it looks: `/deploy` writes it into every node's ``.env``, so a launcher
    that sets the environment and passes no name at all is an ordinary case —
    and without it such a node comes up under a random name that main has never
    heard of and will never address.
    """
    for candidate in (
        getattr(args, "node", None),
        getattr(args, "name", None),
        os.getenv("WACTORZ_NODE"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return f"node-{uuid.uuid4().hex[:6]}"


def broker_host(args: argparse.Namespace) -> str:
    """Which broker this node dials.

    ``WACTORZ_BROKER`` before ``MQTT_HOST``: a node's ``.env`` names the broker
    from where the *node* sits, which is rarely what main calls it — main's own
    ``MQTT_HOST`` is often ``localhost``, and a node that adopted that would
    connect to itself.
    """
    return args.mqtt_broker or args.broker or os.getenv("WACTORZ_BROKER") or CONFIG.mqtt_host


def broker_port(args: argparse.Namespace) -> int:
    """Which port it dials, with TLS deciding the default as it does elsewhere."""
    chosen = args.mqtt_port or args.port or os.getenv("WACTORZ_PORT")
    return int(chosen) if chosen else CONFIG.mqtt_port


def tls_problem() -> str:
    """Why this node's TLS context cannot be built, or "" when it can or TLS is off."""
    if not tls_enabled(CONFIG.mqtt_tls):
        return ""
    try:
        client_context(CONFIG.mqtt_tls_ca, CONFIG.mqtt_tls_check_hostname)
    except OSError as exc:
        return str(exc)
    return ""


def check_startable(node_name: str) -> None:
    """Refuse to start a node that could never work, saying which way."""
    problem = deploy_name_error(node_name)
    if problem:
        logger.error("[runner] Refusing to start: %s Rename the node and redeploy.", problem)
        raise SystemExit(2)

    tls = tls_problem()
    if tls:
        logger.error(
            "[runner] Refusing to start: MQTT_TLS is on, but the CA to verify the broker "
            "with could not be loaded (%s). Deploy the node again, or correct MQTT_TLS_CA "
            "in ~/wactorz/.env.",
            tls,
        )
        raise SystemExit(2)


def run(args: argparse.Namespace) -> None:
    """Run this process as a node until it is asked to stop."""
    node_name = node_name_from(args)
    check_startable(node_name)

    runner = NodeRunner(broker=broker_host(args), port=broker_port(args), node_name=node_name)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler() -> None:
        logger.info("[runner] Shutdown signal received.")
        loop.create_task(runner.shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, AttributeError):
            # Windows supports add_signal_handler for almost nothing; the
            # process is stopped from outside there instead.
            pass

    try:
        loop.run_until_complete(runner.run())
    finally:
        loop.close()


def get_args(argv: list[str] | None = None) -> argparse.Namespace:
    """The arguments ``wactorz-node`` takes: the name, the broker, and a log level.

    Its own parser rather than the server's. The server's accepts every flag a
    server takes and ignores the rest, which is what let an older release start
    as a *server* when handed ``--node``: the flag was unknown, so it was
    dropped, and the process came up as a second main on the same broker. A
    command that exists only in releases carrying the node runtime cannot be
    misread that way -- an older install has no ``wactorz-node`` at all, and the
    unit fails with "command not found" instead of starting the wrong thing.
    """
    parser = argparse.ArgumentParser(
        prog="wactorz-node",
        description="Run this machine as a Wactorz edge node.",
    )
    parser.add_argument(
        "--node",
        metavar="NAME",
        default=None,
        help="This node's name. Defaults to $WACTORZ_NODE, which /deploy writes into ~/wactorz/.env.",
    )
    parser.add_argument(
        "--mqtt-broker",
        default=None,
        help="The broker's address as seen from this node. Defaults to $WACTORZ_BROKER.",
    )
    parser.add_argument(
        "--mqtt-port", type=int, default=None, help="The broker's port. Defaults to $WACTORZ_PORT."
    )
    parser.add_argument("--loglevel", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    # The spellings the server's parser accepts for the same things, so `run`
    # reads one shape whichever command started it.
    args = parser.parse_args(argv)
    args.name = None
    args.broker = None
    args.port = None
    return args


def main(argv: list[str] | None = None) -> None:
    """Entry point for the ``wactorz-node`` console script."""
    args = get_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=getattr(logging, str(args.loglevel).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    run(args)
