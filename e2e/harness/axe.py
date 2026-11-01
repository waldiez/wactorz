"""Accessibility checks with axe, on the page as the browser lays it out.

The rule is the frontend unit tests' (``frontend/src/__tests__/axe.ts``): a
serious or critical finding fails, unless it is one accepted, with its reason,
in ``axe-accepted.json`` beside this file. Here, unlike there, the page is laid
out, so colour contrast and landmarks are judged too.

axe comes from the frontend's own dependencies, so both run the same version.
It is loaded through Playwright rather than as a ``<script>`` tag: the dashboard
allows only scripts carrying its nonce, and the suite keeps testing that policy
rather than switching it off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from playwright.sync_api import Page

AXE_SCRIPT = (
    Path(__file__).resolve().parents[2] / "frontend" / "node_modules" / "axe-core" / "axe.min.js"
)
ACCEPTED_FILE = Path(__file__).with_name("axe-accepted.json")

#: Impacts that fail a check.
BLOCKING = frozenset({"serious", "critical"})

#: Fades are waited out first: a toast sliding in or a message appearing is half
#: transparent, and its colours would be judged as they never stay. Only what
#: changes opacity and ends: a toast's countdown bar moves for seconds, and a
#: blink or the waiting dots never stop.
_RUN = """
async () => {
    const fadesIn = a => a instanceof CSSTransition
        ? a.transitionProperty === "opacity"
        : a.effect.getKeyframes().some(frame => "opacity" in frame);
    const fading = document.getAnimations().filter(
        a => a.playState === "running"
            && a.effect
            && a.effect.getComputedTiming().iterations !== Infinity
            && fadesIn(a),
    );
    await Promise.allSettled(fading.map(a => a.finished));
    const result = await axe.run(document, { resultTypes: ["violations"] });
    return result.violations.flatMap(v => v.nodes.map(n => ({
        rule: v.id,
        impact: v.impact || "",
        help: v.help,
        target: n.target.join(" "),
        detail: ([...n.any, ...n.all, ...n.none][0] || {}).message || "",
    })));
}
"""


class Finding(TypedDict):
    """One element axe found wrong, and by which rule."""

    rule: str
    impact: str
    help: str
    target: str
    #: What axe measured, such as a contrast ratio and its two colours.
    detail: str


class Accepted(TypedDict):
    """A finding someone decided to live with, and why."""

    rule: str
    selector: str
    reason: str


def accepted() -> list[Accepted]:
    """The findings accepted for now, each with its reason."""
    return json.loads(ACCEPTED_FILE.read_text(encoding="utf-8"))


def is_accepted(finding: Finding, accepted_findings: list[Accepted]) -> bool:
    """Whether ``finding`` is one of ``accepted_findings``: its rule, on an element it names."""
    return any(
        a["rule"] == finding["rule"] and a["selector"] in finding["target"]
        for a in accepted_findings
    )


def blocking_findings(page: Page) -> list[str]:
    """The serious and critical findings on ``page`` now that nobody accepted, one line each."""
    if not AXE_SCRIPT.is_file():
        raise AssertionError(f"axe is not installed: run `bun install` in frontend ({AXE_SCRIPT})")
    if not page.evaluate("() => typeof window.axe === 'object'"):
        page.evaluate(AXE_SCRIPT.read_text(encoding="utf-8"))
    found: list[Finding] = page.evaluate(_RUN)
    allowed = accepted()
    return [
        f"{f['rule']} ({f['impact']}): {f['target']} — {f['detail'] or f['help']}"
        for f in found
        if f["impact"] in BLOCKING and not is_accepted(f, allowed)
    ]
