"""Values JSON cannot hold, written so that they come back as what they were.

An agent's state is JSON wherever it leaves the process that wrote it: in a
node's state file, and in the snapshot a migration carries. JSON has no date,
no set and no tuple, and its object keys are strings. Left to `json.dumps`, a
`datetime` or a `set` is refused, so the key is dropped, and a tuple or a dict
keyed by numbers is turned into something else without a word: an agent
counting per hour would find its keys had become strings after a move.

So each such value is written as a small object naming its kind, and read back
into the value itself:

- ``datetime``, ``date`` and ``time``, by their ISO form. A timezone travels as
  its offset from UTC: a named zone comes back as a fixed offset.
- ``timedelta``, by its days, seconds and microseconds, which keeps it exact.
- ``set``, ``frozenset`` and ``tuple``, by their items. A named tuple comes back
  as a plain tuple.
- a ``dict`` whose keys are not all strings, or that holds the marker key
  itself, by its pairs.

Lists and string-keyed dicts are walked, so a value inside one is handled too.
Anything else that JSON cannot hold raises `TypeError`, as `json.dumps` does,
so the callers that drop or refuse such a key keep doing so; a model, an array
or bytes are blobs, which `wactorz.core.blobs` keeps beside the JSON.

What is plain JSON is left exactly as it is, so a file or a snapshot written
before any of this reads the same.
"""

import datetime as dt
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: The key of the object that stands for a value JSON cannot hold, naming its kind.
VALUE_MARK = "__wactorz_value__"


def encode(value: Any) -> Any:
    """``value`` as plain JSON, each value JSON cannot hold written as a marked object.

    Raises `TypeError` for a value with no form here, and `RecursionError` for
    one that contains itself.
    """
    # bool and int first: they are what most state is, and need nothing.
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        if VALUE_MARK not in value and all(isinstance(key, str) for key in value):
            return {key: encode(item) for key, item in value.items()}
        return {
            VALUE_MARK: "dict",
            "items": [[encode(key), encode(item)] for key, item in value.items()],
        }
    if isinstance(value, tuple):
        return {VALUE_MARK: "tuple", "items": [encode(item) for item in value]}
    if isinstance(value, (set, frozenset)):
        kind = "frozenset" if isinstance(value, frozenset) else "set"
        return {VALUE_MARK: kind, "items": [encode(item) for item in _in_order(value)]}
    # datetime before date: a datetime is a date as well.
    if isinstance(value, dt.datetime):
        return {VALUE_MARK: "datetime", "iso": value.isoformat()}
    if isinstance(value, dt.date):
        return {VALUE_MARK: "date", "iso": value.isoformat()}
    if isinstance(value, dt.time):
        return {VALUE_MARK: "time", "iso": value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {
            VALUE_MARK: "timedelta",
            "days": value.days,
            "seconds": value.seconds,
            "microseconds": value.microseconds,
        }
    raise TypeError(f"a {type(value).__name__} has no JSON form")


def json_form(value: Any) -> Any:
    """``value`` as it is written to JSON, or `NotJson` if it cannot be.

    What the callers deciding which keys to keep, send or refuse ask, so that
    they all draw the line in the same place.
    """
    try:
        encoded = encode(value)
        json.dumps(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise NotJson(str(exc)) from exc
    return encoded


class NotJson(Exception):
    """A value that cannot be written as JSON, even marked."""


def decode(value: Any) -> Any:
    """``value`` with each marked object read back into the value it stands for.

    A marked object of a kind this side does not know is left as it is, as a
    newer release may write kinds an older one cannot read. Raises
    `ValueError`, `TypeError` or `KeyError` for a marked object that is not
    well formed.
    """
    if isinstance(value, list):
        return [decode(item) for item in value]
    if isinstance(value, dict):
        kind = value.get(VALUE_MARK)
        reader = _READERS.get(kind) if isinstance(kind, str) else None
        if reader is not None:
            return reader(value)
        return {key: decode(item) for key, item in value.items()}
    return value


def decode_state(values: Mapping[str, Any]) -> dict[str, Any]:
    """An agent's state with each value read back, keeping as it is one that is not well formed.

    One damaged value costs that value its type, not the agent its state.
    """
    decoded: dict[str, Any] = {}
    for key, value in values.items():
        try:
            decoded[key] = decode(value)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "State key %r could not be read back, and is kept as stored: %s", key, exc
            )
            decoded[key] = value
    return decoded


def _in_order(items: Iterable[Any]) -> list[Any]:
    """The items of a set in a stable order where they have one.

    So a set that has not changed is written the same way twice, and a file
    holding it is not rewritten for nothing.
    """
    try:
        return sorted(items)
    except TypeError:
        return list(items)


def _items(value: Mapping[str, Any]) -> list[Any]:
    items = value["items"]
    if not isinstance(items, list):
        raise TypeError(f"the items of a {value[VALUE_MARK]} are not a list")
    return [decode(item) for item in items]


def _pairs(value: Mapping[str, Any]) -> dict[Any, Any]:
    return {_hashable(key): item for key, item in _items(value)}


def _hashable(key: Any) -> Any:
    """A decoded key, which must be usable as one."""
    hash(key)
    return key


_READERS: dict[str, Callable[[Mapping[str, Any]], Any]] = {
    "tuple": lambda value: tuple(_items(value)),
    "set": lambda value: set(_items(value)),
    "frozenset": lambda value: frozenset(_items(value)),
    "dict": _pairs,
    "datetime": lambda value: dt.datetime.fromisoformat(value["iso"]),
    "date": lambda value: dt.date.fromisoformat(value["iso"]),
    "time": lambda value: dt.time.fromisoformat(value["iso"]),
    "timedelta": lambda value: dt.timedelta(
        days=value["days"], seconds=value["seconds"], microseconds=value["microseconds"]
    ),
}
