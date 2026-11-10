# SPDX-License-Identifier: Apache-2.0
"""Another Wactorz install on this one's namespace: heard from its health, and said once.

Two installs on one broker under the same namespace -- or both under none --
share every control topic: each hears the other's agents and nodes, and either
may drive them. It is how a dev instance pointed at the house broker goes wrong
on its first day. Each main's monitor stamps the ``system/health`` it publishes
with `install_id()`, so a health message on this install's own topic stamped by
another install is that case, and main says so in chat, with what to set.

A health message with no stamp, from a release before it, cannot be told apart
and is let be.
"""

import json
import logging

from ...core.mqtt import install_id
from ...core.topics import NAMESPACE, system_topic
from ..monitor_agent import HEALTH_INSTALL_FIELD
from .hosts import NodeHost

logger: logging.Logger = logging.getLogger(__name__)

#: Where every main's monitor reports the install's health, under this install's namespace.
HEALTH_TOPIC = system_topic("health")


class OtherInstalls:
    """Says once per install that another one shares this install's topics."""

    def __init__(self, host: NodeHost | None) -> None:
        self.host = host
        #: The installs already said, by id.
        self.reported: set[str] = set()

    def receive(self, payload: bytes | None) -> None:
        """Take one health message off this install's health topic."""
        try:
            data = json.loads(payload or b"")
        except ValueError:
            return
        other = data.get(HEALTH_INSTALL_FIELD) if isinstance(data, dict) else None
        if not isinstance(other, str) or not other or other == install_id():
            return
        if other in self.reported or self.host is None:
            return
        self.reported.add(other)
        where = f"namespace '{NAMESPACE}'" if NAMESPACE else "no namespace"
        logger.warning(
            "[main] Another install (%s) shares this install's topics (%s)", other, where
        )
        self.host._queue_notification(
            {
                "severity": "warning",
                "about": f"second-install:{other}",
                "message": (
                    f"Another Wactorz install ({other[:8]}) is on this broker under {where}, "
                    "the same as this one, so each hears the other's agents and nodes and "
                    "either can drive them. Give one of them a namespace of its own: set "
                    "WACTORZ_NAMESPACE (for example WACTORZ_NAMESPACE=dev) where it runs, "
                    "restart it, and deploy its nodes again."
                ),
            }
        )
