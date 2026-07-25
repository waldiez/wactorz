"""Logo picking, the snapshot DTO, static metadata and the brand theme.

Small modules, but the logo width rule is what keeps the home tab from wrapping
into garbage on a narrow terminal, and the theme colours are contractually tied
to the web dashboard's — the TUI and the SPA are meant to read as one product.
"""

# pylint: disable=missing-function-docstring,protected-access

from dataclasses import is_dataclass

import pytest

from wactorz.tui import logo, meta, theme
from wactorz.tui.snapshot import HostStats, Snapshot

# ── logo ────────────────────────────────────────────────────────────────────


def test_the_full_wordmark_is_used_when_it_fits() -> None:
    assert logo.pick_logo(logo._FULL_WIDTH) == logo.LOGO_FULL


def test_one_column_short_falls_back_to_compact() -> None:
    assert logo.pick_logo(logo._FULL_WIDTH - 1) == logo.LOGO_COMPACT


def test_a_tiny_terminal_still_gets_a_wordmark() -> None:
    assert logo.pick_logo(0) == logo.LOGO_COMPACT


def test_the_declared_full_width_matches_the_art() -> None:
    # _FULL_WIDTH is hand-kept in sync with the ASCII above it; if someone edits
    # the art without updating it, the picker chooses wrong.
    assert max(len(line) for line in logo.LOGO_FULL.splitlines()) == logo._FULL_WIDTH


def test_the_compact_wordmark_actually_is_narrower() -> None:
    compact = max(len(line) for line in logo.LOGO_COMPACT.splitlines())
    assert compact < logo._FULL_WIDTH


def test_neither_wordmark_has_blank_leading_or_trailing_lines() -> None:
    for art in (logo.LOGO_FULL, logo.LOGO_COMPACT):
        assert art.splitlines()[0].strip()
        assert art.splitlines()[-1].strip()


# ── snapshot ────────────────────────────────────────────────────────────────


def test_an_empty_snapshot_renders_as_all_zeros() -> None:
    # Every pane must survive the very first tick, before any data arrives.
    snap = Snapshot()
    assert snap.agent_count == 0
    assert snap.node_count == 0
    assert snap.cost_usd == 0.0
    assert snap.broker_connected is False
    assert snap.host.cpu_pct == 0.0


def test_counts_follow_the_lists() -> None:
    snap = Snapshot(agents=[{"name": "a"}, {"name": "b"}], nodes=[{"id": "n1"}])
    assert (snap.agent_count, snap.node_count) == (2, 1)


def test_each_snapshot_gets_its_own_lists() -> None:
    # A shared mutable default would leak agents between refresh ticks.
    first, second = Snapshot(), Snapshot()
    first.agents.append({"name": "a"})
    assert not second.agents
    assert first.host is not second.host


def test_host_stats_are_grouped_not_flattened() -> None:
    assert is_dataclass(HostStats)
    assert isinstance(Snapshot().host, HostStats)


# ── meta ────────────────────────────────────────────────────────────────────


def test_a_version_is_always_available() -> None:
    assert isinstance(meta.VERSION, str)
    assert meta.VERSION


def test_the_subtitle_describes_the_product() -> None:
    assert meta.SUBTITLE == "actor-model multi-agent runtime"


# ── theme ───────────────────────────────────────────────────────────────────


def test_the_theme_is_registered_under_a_stable_name() -> None:
    # app.py sets `self.theme = "wactorz"` by this exact string.
    assert theme.WACTORZ_THEME.name == "wactorz"


def test_the_theme_is_dark() -> None:
    assert theme.WACTORZ_THEME.dark is True


@pytest.mark.parametrize(
    "colour",
    [theme.INDIGO, theme.VIOLET, theme.NODE, theme.SIGNATURE, theme.MINT, theme.INK, theme.VOID],
)
def test_brand_colours_are_valid_hex(colour: str) -> None:
    assert colour.startswith("#")
    assert len(colour) == 7
    int(colour[1:], 16)  # raises if not hex


def test_the_theme_uses_the_brand_palette() -> None:
    # These are sourced from the favicon and the dashboard CSS, not invented —
    # drifting from them splits the product's identity in two.
    assert theme.WACTORZ_THEME.primary == theme.INDIGO
    assert theme.WACTORZ_THEME.accent == theme.VIOLET
    assert theme.WACTORZ_THEME.background == theme.VOID
    assert theme.WACTORZ_THEME.foreground == theme.INK


def test_the_signature_colour_is_distinct_from_the_node_colour() -> None:
    # SIGNATURE marks the single centre dot and is used exactly once; if it
    # collided with NODE the accent would disappear into the rest of the mark.
    assert theme.SIGNATURE != theme.NODE
