"""The process that runs Wactorz agents on an edge node.

A node is a machine that is not the one main runs on — a Raspberry Pi, a VM, a
camera host. It connects to the same MQTT broker, listens for spawn commands on
``nodes/<name>/…`` and runs the agents it is sent, which heartbeat back and
appear on the central dashboard exactly like local ones.

The package is installed on the node and started with ``wactorz --node <name>``.
Everything here therefore imports the rest of ``wactorz`` freely, and that is the
point of it: a node runs the same :class:`~wactorz.agents.DynamicAgent`, over the
same MQTT helpers, with the same TLS and control-message signing rules as main.
There is no second implementation of the agent contract to keep in step.

Layering, the same shape the agent packages use: a module here may import from
``core/`` and ``agents/``, and neither may import back from ``node/``. The one
thing this package owns that the rest does not is the shape of the process — a
publish queue that must not grow without bound on a machine with 512MB of RAM,
a control plane whose messages arrive over the broker rather than from a
registry, and a supervisor that answers to main rather than to an ``app``.

What still differs from main, and why:

- **Publishing goes through a bounded queue** (:mod:`.publishing`) rather than
  the server's SQLite-backed outbox. A node has no outbox, so a dropped message
  is gone; the queue is sized and ordered so only a long outage reaches that.
- **Agent state is JSON on disk** (:mod:`.state`), not the server's pickle. A
  node's state file is also what a migration ships over MQTT, and JSON is what
  survives the node and main running different Python versions.
- **The LLM lives on main.** An agent here calls ``agent.llm.chat(...)`` as it
  would anywhere, and the call is routed over the broker, so no API key is ever
  deployed to a node.
"""

from .agent import NodeAgent
from .runner import NodeRunner
from .signing import ControlGuard

__all__ = [
    "ControlGuard",
    "NodeAgent",
    "NodeRunner",
]
