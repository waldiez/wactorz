"""``python -m wactorz.node.compose_env``: the sandbox node's settings, in the compose stack.

In the compose stack the sandbox node is a container, not a machine ``/deploy``
reaches over SSH, so nothing would otherwise write it the ``.env`` a deployed
node gets. A one-shot container runs this instead, with main's settings and
state, and writes into a volume only the sandbox container mounts:

- ``.env``, as :func:`wactorz.core.node_env.node_env_text` writes it for any
  node: its name, the broker, its signing key and its broker account;
- ``ca.crt``, the CA the broker's certificate is checked against, when main
  reaches the broker over TLS.

Nothing else of main's crosses: the sandbox container never mounts main's
state, and reads only what is written here.
"""

import argparse
import logging
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from ..config import CONFIG, MODEL_CODE_NODE
from ..core.mqtt_tls import SYSTEM_TRUST, checks_hostname, generated_ca_path, tls_enabled
from ..core.node_env import UnusableNodeAccountError, node_account, node_env_text

logger = logging.getLogger(__name__)

#: The node's name when ``WACTORZ_MODEL_CODE_NODE`` names none.
DEFAULT_NODE = "sandbox"

#: The CA's file name in the output directory.
CA_NAME = "ca.crt"


def write(out: Path, broker: str, node: str) -> Path:
    """Write the node's ``.env`` (and the CA, with TLS) into ``out``; return the ``.env``'s path."""
    out.mkdir(parents=True, exist_ok=True)
    tls_ca: str | None = None
    if tls_enabled(CONFIG.mqtt_tls):
        setting = CONFIG.mqtt_tls_ca.strip()
        if setting.lower() == SYSTEM_TRUST:
            tls_ca = SYSTEM_TRUST
        else:
            source = Path(setting).expanduser() if setting else generated_ca_path()
            shutil.copyfile(source, out / CA_NAME)
            tls_ca = str(out / CA_NAME)
    username, password = node_account(node)
    text = node_env_text(
        node,
        broker,
        # The port main dials, which is the TLS one when TLS is on.
        CONFIG.mqtt_port,
        username=username,
        password=password,
        tls_ca=tls_ca,
        check_hostname=checks_hostname(CONFIG.mqtt_tls_ca, CONFIG.mqtt_tls_check_hostname),
    )
    env = out / ".env"
    pending = out / ".env.new"
    # Created private rather than tightened after: it holds the node's broker
    # password and signing key.
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    pending.replace(env)
    return env


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit status."""
    logging.basicConfig(level=logging.INFO, format="[sandbox-env] %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m wactorz.node.compose_env",
        description="Write the compose stack's sandbox node its settings.",
    )
    parser.add_argument("--out", required=True, help="The directory the sandbox container reads.")
    parser.add_argument("--broker", default="mosquitto", help="The broker, as the node reaches it.")
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    node = MODEL_CODE_NODE or DEFAULT_NODE
    try:
        env = write(Path(args.out), args.broker, node)
    except (OSError, UnusableNodeAccountError) as exc:
        logger.error("Could not write the settings for node %r: %s", node, exc)  # noqa: TRY400  # a one-shot's answer, not a trace
        return 1
    logger.info("Wrote the settings for node %r to %s", node, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
