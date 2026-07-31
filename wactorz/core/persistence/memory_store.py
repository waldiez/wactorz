"""Process-local store for ephemeral values, with optional per-key expiry.

Holds the high-frequency values that normal operation regenerates — observed
samples, agent metrics, heartbeat state — so losing them on restart costs
nothing. Anything that must survive a restart belongs in SQLite instead; see
``EPHEMERAL_KEYS`` and ``SQLITE_KEYS`` in :mod:`.api` for the routing.

Values are JSON round-tripped even though nothing crosses a process boundary.
That enforces the "must be JSON-serialisable" contract, so a value cannot work
here and then fail if a key is later routed to a durable store — and it keeps
types stable, since a datetime comes back as a string either way.
"""

import fnmatch
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class MemoryStore:
    """A dict with optional per-key expiry."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store a JSON-serialisable value, optionally expiring after ``ttl`` seconds."""
        self._data[key] = {
            "value": json.dumps(value, default=str),
            "expires": time.time() + ttl if ttl else None,
        }

    def get(self, key: str, default: Any = None) -> Any:
        """The stored value, or ``default`` if absent or expired.

        Expiry is lazy: an expired key is dropped when read, not on a timer, so
        a key never read again occupies its slot until the process ends.
        """
        entry = self._data.get(key)
        if entry is None:
            return default
        if entry.get("expires") and time.time() > entry["expires"]:
            del self._data[key]
            return default
        try:
            return json.loads(entry["value"])
        except (json.JSONDecodeError, TypeError):
            return entry["value"]

    def delete(self, key: str) -> None:
        """Remove a key. Missing keys are not an error."""
        self._data.pop(key, None)

    def keys(self, pattern: str = "*") -> list[str]:
        """Keys matching a glob, excluding any that have expired."""
        now = time.time()
        return [
            k
            for k, v in self._data.items()
            if fnmatch.fnmatch(k, pattern) and (not v.get("expires") or v["expires"] > now)
        ]

    def clear(self) -> None:
        """Drop every key. Everything held here is regenerated in use."""
        self._data.clear()
