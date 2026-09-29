"""Compatibility shim for the single-file node runner.

A node used to be given a copy of this module and nothing else, so it carried
its own retelling of the MQTT helpers, the TLS rule, the signing check and the
agent contract. A node now installs the ``wactorz`` package and starts with
``wactorz --node <name>``, and all of that is the package's own code — see
:mod:`wactorz.node`.

What is left here is the two things an existing deployment still reaches for:
the module path, so ``python -m wactorz.remote_runner --name rpi`` keeps working
while units are updated, and the names other code imported from it.
"""

from .cli import main
from .node import NodeAgent, NodeRunner
from .node.cli import run as run_node
from .node.runner import NODE_RUNTIME

__all__ = ["NODE_RUNTIME", "NodeAgent", "NodeRunner", "main", "run_node"]


if __name__ == "__main__":
    main()
