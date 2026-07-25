"""Shared presentation helpers.

Pure functions, so they get exact assertions — these are the DRY backstop every
pane renders through, and `mask_secret` is the one guarding the settings tab
from printing a credential.
"""

# pylint: disable=missing-function-docstring

import logging

import pytest
from rich.text import Text

from wactorz.tui import format as fmt

# ── colour maps ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "style"),
    [
        ("running", "green"),
        ("idle", "green"),
        ("starting", "cyan"),
        ("paused", "yellow"),
        ("stopping", "yellow"),
        ("stopped", "red"),
        ("failed", "bold red"),
    ],
)
def test_each_lifecycle_state_has_its_own_colour(state: str, style: str) -> None:
    assert fmt.state_text(state).style == style


def test_state_matching_is_case_insensitive() -> None:
    # get_status() casing is not guaranteed across agent implementations.
    assert fmt.state_text("RUNNING").style == "green"


def test_an_unknown_state_renders_unstyled_but_readable() -> None:
    text = fmt.state_text("bewildered")
    assert text.style == ""
    assert text.plain == "bewildered"


def test_the_state_label_is_preserved_verbatim() -> None:
    assert fmt.state_text("Running").plain == "Running"


@pytest.mark.parametrize(
    ("levelno", "style"),
    [
        (logging.CRITICAL, "bold red"),
        (logging.ERROR, "red"),
        (logging.WARNING, "yellow"),
        (logging.INFO, ""),
        (logging.DEBUG, "dim"),
    ],
)
def test_log_lines_are_coloured_by_level(levelno: int, style: str) -> None:
    assert fmt.log_line(levelno, "hello").style == style


def test_a_custom_log_level_still_renders() -> None:
    line = fmt.log_line(25, "notice")
    assert isinstance(line, Text)
    assert line.plain == "notice"


def test_the_level_cycle_only_widens_downward() -> None:
    # F2 walks INFO -> WARNING -> ERROR; DEBUG is never captured by the bridge.
    assert fmt.LEVEL_CYCLE == [logging.INFO, logging.WARNING, logging.ERROR]


# ── humanizers ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B/s"),
        (359, "359 B/s"),
        (1023, "1023 B/s"),
        (1024, "1.0 KB/s"),
        (1536, "1.5 KB/s"),
        (1024**2, "1.0 MB/s"),
        (3.4 * 1024**2, "3.4 MB/s"),
        (1024**3, "1.0 GB/s"),
        (5 * 1024**4, "5120.0 GB/s"),  # no unit beyond GB — must not crash
    ],
)
def test_human_bytes(value: float, expected: str) -> None:
    assert fmt.human_bytes(value) == expected


def test_bytes_below_a_kilobyte_have_no_decimals() -> None:
    # A per-second byte count churns every tick; decimals would just flicker.
    assert fmt.human_bytes(999) == "999 B/s"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0m"),
        (59, "0m"),
        (60, "1m"),
        (12 * 60, "12m"),
        (3600, "1h 0m"),
        (5 * 3600 + 38 * 60, "5h 38m"),
        (86400, "1d 0h"),
        (2 * 86400 + 4 * 3600, "2d 4h"),
    ],
)
def test_human_duration(seconds: float, expected: object) -> None:
    assert fmt.human_duration(seconds) == expected


def test_duration_drops_the_finest_unit_as_it_grows() -> None:
    # Days show hours (not minutes); hours show minutes (not seconds).
    assert fmt.human_duration(86400 + 3600 + 59 * 60) == "1d 1h"


def test_a_fractional_duration_is_truncated() -> None:
    assert fmt.human_duration(119.9) == "1m"


# ── masking ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("secret", ["sk-abcdef123456", "x", 12345, ["nonempty"]])
def test_a_set_secret_is_never_echoed(secret: object) -> None:
    rendered = fmt.mask_secret(secret)
    assert str(secret) not in rendered
    assert "set" in rendered


@pytest.mark.parametrize("empty", [None, "", 0, [], {}])
def test_an_unset_secret_says_so(empty: object) -> None:
    assert fmt.mask_secret(empty) == "[dim](not set)[/]"


def test_a_plain_value_is_shown() -> None:
    assert fmt.plain("broker.local") == "[cyan]broker.local[/]"


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_plain_value_becomes_a_dash(empty: object) -> None:
    assert fmt.plain(empty) == "[dim]—[/]"


def test_zero_is_a_real_plain_value() -> None:
    # Port 0 / count 0 are meaningful; only None and "" are "unset".
    assert fmt.plain(0) == "[cyan]0[/]"
