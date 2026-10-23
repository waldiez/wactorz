"""Values JSON cannot hold come back as what they were.

An agent's state is JSON wherever it leaves its process: a node's state file and
a migration's snapshot. A date or a set would otherwise be dropped there, and a
tuple or a dict keyed by numbers quietly turned into something else.
"""

import datetime as dt
import json
import logging
from collections import namedtuple
from typing import Any

import pytest

from wactorz.core.state_codec import (
    VALUE_MARK,
    NotJson,
    decode,
    decode_state,
    encode,
    json_form,
)


def through_json(value: Any) -> Any:
    """``value`` after a trip through a JSON file or message."""
    return decode(json.loads(json.dumps(encode(value))))


@pytest.mark.parametrize(
    "value",
    [
        # Naive, as `datetime.now()` gives and agents store.
        dt.datetime(2026, 10, 11, 9, 30, 15, 123456),  # noqa: DTZ001
        dt.datetime(2026, 10, 11, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=3))),
        dt.date(2026, 10, 11),
        dt.time(23, 59, 59, 999999),
        dt.timedelta(days=-2, seconds=5, microseconds=7),
        (1, "two", 3.0),
        {1, 2, 3},
        frozenset({"a", "b"}),
        {0: 3, 23: 5},
        {(1, 2): "pair"},
    ],
    # Named rather than repr'd: a set's repr depends on the process's hash seed,
    # and xdist workers must collect the same names.
    ids=[
        "naive datetime",
        "aware datetime",
        "date",
        "time",
        "timedelta",
        "tuple",
        "set",
        "frozenset",
        "int keys",
        "tuple keys",
    ],
)
def test_a_value_json_cannot_hold_comes_back_as_it_was(value: Any) -> None:
    back = through_json(value)

    assert back == value
    assert type(back) is type(value)


def test_a_timezone_comes_back_as_its_offset() -> None:
    when = dt.datetime(2026, 10, 11, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=3)))

    assert through_json(when).utcoffset() == dt.timedelta(hours=3)


def test_values_inside_lists_and_dicts_come_back_too() -> None:
    state = {
        "seen": [
            dt.date(2026, 1, 1),
            {"at": dt.datetime(2026, 1, 1, 12, tzinfo=dt.timezone.utc), "ids": {3, 1}},
        ],
        "per_hour": {"weekday": {9: 4, 17: 6}},
    }

    assert through_json(state) == state
    assert type(through_json(state)["per_hour"]["weekday"]) is dict


def test_plain_json_is_left_exactly_as_it_is() -> None:
    # So a file or a snapshot written before any of this reads the same.
    plain = {"count": 3, "ratio": 0.5, "name": "x", "on": True, "none": None, "list": [1, [2]]}

    assert encode(plain) == plain
    assert decode(plain) == plain


def test_a_dict_holding_the_marker_key_is_not_mistaken_for_a_value() -> None:
    odd = {VALUE_MARK: "datetime", "iso": "not a date"}

    assert through_json(odd) == odd


def test_a_set_is_written_the_same_way_each_time() -> None:
    # A file holding a set that did not change is then not rewritten.
    assert json.dumps(encode({"b", "c", "a"})) == json.dumps(encode({"c", "a", "b"}))


def test_a_set_of_mixed_kinds_still_encodes() -> None:
    mixed = {1, "one", (1,)}

    assert through_json(mixed) == mixed


def test_a_named_tuple_comes_back_as_a_tuple() -> None:
    point = namedtuple("Point", "x y")(1, 2)

    assert through_json(point) == (1, 2)


@pytest.mark.parametrize("value", [object(), b"raw", [1, object()], {"capture": object()}])
def test_what_has_no_json_form_is_refused(value: Any) -> None:
    # Bytes go as a blob, beside the JSON, not inside it.
    with pytest.raises(NotJson):
        json_form(value)


def test_a_value_that_contains_itself_is_refused() -> None:
    loop: list[Any] = []
    loop.append(loop)

    with pytest.raises(NotJson):
        json_form(loop)


def test_a_kind_this_side_does_not_know_is_left_as_it_is() -> None:
    # A newer release may write kinds an older one cannot read.
    future = {VALUE_MARK: "decimal", "text": "1.10"}

    assert decode(future) == future


def test_one_damaged_value_costs_only_itself(caplog: pytest.LogCaptureFixture) -> None:
    damaged = {VALUE_MARK: "datetime", "iso": "yesterday"}

    with caplog.at_level(logging.WARNING, logger="wactorz.core.state_codec"):
        state = decode_state({"count": 3, "when": damaged})

    assert state == {"count": 3, "when": damaged}
    assert "'when'" in caplog.text
