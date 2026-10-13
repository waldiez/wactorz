"""Encoding a value for a store that keeps JSON.

SQLite and the in-memory store keep values as JSON text. A value JSON cannot
represent — a datetime, a numpy float, a set — is refused at the write, naming
the key, rather than written as its ``str()``: stored that way it would come back
as a string after the next read, and the code reading it would fail somewhere far
from the line that wrote it.
"""

import json
from typing import Any


class NotJsonError(TypeError):
    """A value given to a JSON-backed store that JSON cannot represent."""

    def __init__(self, owner: str, key: str, value: Any, cause: Exception) -> None:
        super().__init__(
            f"{owner}: '{key}' is kept as JSON, and a {type(value).__name__} cannot be "
            f"({cause}). Convert it first: a datetime to .isoformat() or a timestamp, a "
            f"numpy number with .item(), a set to a list."
        )


def encode(owner: str, key: str, value: Any) -> str:
    """``value`` as JSON text, or `NotJsonError` naming ``owner`` and ``key``."""
    try:
        return json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise NotJsonError(owner, key, value, exc) from exc
