"""The three ways a scenario can be executed, and the one thing they may not change.

A profile decides pacing, capture and which provider answers. It does not decide
what is asserted: every scenario runs its assertions under every profile, the
demo included. That is the whole reason a demo recording is worth anything — it
is a recording of a passing test, so the product moving makes the scenario go red
before anyone re-records it.

    test      headless - condition waits  - no capture - fake model
    rehearse  headed   - minimum dwell    - video      - fake model
    demo      headed   - minimum dwell    - video      - real model

`dwell` is the only timing a scenario may express, and it is expressed as an
intent (``dwell="readable"``) rather than a number of seconds. The mapping from
intent to seconds lives here, so changing how long a recording lingers is one
edit in one file rather than a sweep through the scenarios.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

#: Named dwell intents, in seconds. `test` maps every one of them to zero.
DWELLS: dict[str, float] = {
    "beat": 0.6,
    "readable": 2.0,
    "settle": 3.5,
}

DEFAULT_PROFILE = "test"


@dataclass(frozen=True)
class Profile:
    """How this run is executed. Frozen: a scenario reads it, never edits it."""

    name: str
    #: Whether the browser is invisible. Headless is not a different browser —
    #: the same page objects and the same assertions run either way.
    headless: bool
    #: Whether `dwell` step options are honoured at all.
    paced: bool
    #: Whether Playwright records video of every scenario that drives a browser.
    video: bool
    #: The provider name handed to the backend as ``LLM_PROVIDER``.
    llm: str
    #: A file in ``e2e/scripts/`` whose JSON is handed to the fake provider as
    #: ``LLM_FAKE_SCRIPT``. Ignored when ``llm`` is not the fake.
    script: str = "default.json"
    #: What the page renders at, and what a recording comes out as. Named on the
    #: profile rather than fixed in the harness so a take can be made at 1080p
    #: without every scenario knowing about it.
    size: tuple[int, int] = (1280, 720)
    #: Extra environment every backend in this run is started with.
    env: dict[str, str] = field(default_factory=dict)

    @property
    def uses_fake_model(self) -> bool:
        return self.llm == "fake"

    def dwell(self, intent: str) -> float:
        """Seconds to linger for this intent — zero under an unpaced profile.

        An unknown intent is zero rather than an error: a scenario that asks to
        linger in a way this profile has no name for should still run and still
        assert, because the assertion is the part that matters.
        """
        if not self.paced:
            return 0.0
        return DWELLS.get(intent, 0.0)


PROFILES: dict[str, Profile] = {
    "test": Profile(
        name="test",
        headless=True,
        paced=False,
        video=False,
        llm="fake",
    ),
    "rehearse": Profile(
        name="rehearse",
        headless=False,
        paced=True,
        video=True,
        llm="fake",
        script="demo.json",
    ),
    "demo": Profile(
        name="demo",
        headless=False,
        paced=True,
        video=True,
        # The provider the project ships against, overridable for a machine
        # configured differently. A default rather than a required variable: a
        # take that silently recorded some other model would be worse than one
        # that recorded the expected model without being asked twice.
        llm=os.getenv("E2E_DEMO_LLM", "anthropic"),
    ),
}


def resolve(name: str | None) -> Profile:
    """The named profile, or the default. An unknown name is an error.

    Refused rather than defaulted, because the failure mode of a silent fallback
    is a `demo` run that quietly recorded the fake model — which is exactly the
    take nobody wants to discover after sharing it.
    """
    chosen = (name or os.getenv("E2E_PROFILE") or DEFAULT_PROFILE).strip().lower()
    if chosen not in PROFILES:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown profile {chosen!r} - choose one of: {known}")
    return PROFILES[chosen]
