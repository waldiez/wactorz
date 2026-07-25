"""The Wactorz brand theme.

Colours are sourced from the existing mark and dashboard — not invented here —
so the TUI, the web dashboard and the "connect-the-dots W" favicon read as one
product: ``frontend/public/favicon.svg`` (the W stroke and its five node dots)
and ``frontend/src/styles/base.css`` (page background/text).
"""

from __future__ import annotations

from textual.theme import Theme

INDIGO = "#6366f1"  # W-stroke, outer half — structural chrome (primary)
VIOLET = "#a78bfa"  # W-stroke, inner half — active/focus states (accent)
NODE = "#22d3ee"  # the dot colour at the W's four outer corners
SIGNATURE = "#f472b6"  # the single dot at the W's centre peak — used once
MINT = "#3dd68c"  # the dashboard's own "good" green (cards.css --accent)
INK = "#e0e8ff"  # dashboard body text
VOID = "#050818"  # dashboard background
DEPTH = "#0d1a38"  # favicon radial-gradient highlight

WACTORZ_THEME = Theme(
    name="wactorz",
    primary=INDIGO,
    secondary="#4f46e5",
    accent=VIOLET,
    success=MINT,
    warning="#ffa62b",
    error="#ba3c5b",
    foreground=INK,
    background=VOID,
    panel=DEPTH,
    dark=True,
)
