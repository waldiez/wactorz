"""Write the broker files the access-list tests run against.

``python -m tests.broker.acl_fixture <directory>`` writes ``node_passwd`` and
``acl`` there, exactly as the server does at startup, for the accounts below,
and again under ``<directory>/unnamed`` as a server with no account writes them.
The node passwords are derived from the install secret under
``WACTORZ_STATE_DIR``, so the tests, given the same directory, derive the same
ones. `scripts/test-broker.sh` runs this and loads the result into the broker.
"""

import sys
from pathlib import Path

from wactorz.core import broker_accounts

#: The server's own account, and one standing for another system on the broker
#: that the operator listed. Their passwords are set by the script.
SERVER = "wactorz"
LISTED = "homeassistant"
#: An account the broker knows and the access list does not name.
UNLISTED = "outsider"
#: A fixture, for a broker that lives for one test run.
PASSWORD = "broker-test-password"

NODES = ("node-a", "node-b")
#: Where the files of a server that connects with no account go.
UNNAMED = "unnamed"
#: What `WACTORZ_NODE_TOPICS` would add: one prefix to use, one to read.
EXTRA_TOPICS = "plant/#, read:weather/#"


def node_topics() -> list[tuple[str, str]]:
    return [*broker_accounts.NODE_TOPICS, *broker_accounts.parse_node_topics(EXTRA_TOPICS)]


def main(directory: str) -> None:
    broker_accounts.write_files(Path(directory), NODES, [SERVER, LISTED], node_topics())
    broker_accounts.write_files(Path(directory) / UNNAMED, NODES, [], node_topics())


if __name__ == "__main__":
    main(sys.argv[1])
