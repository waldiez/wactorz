"""The live date/time context handed to models.

Shared by LLMAgent and by actors that have no LLM, which is why it lives with
neither.
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ── Live date/time context (shared by LLMAgent and non-LLMAgent actors) ───────


def resolve_now(tz_name: str | None = None) -> datetime:
    """Current time in the first usable timezone of:
        tz_name (e.g. a user's pref_timezone) > WACTORZ_TZ env > TZ env > local.

    Named zones are resolved with zoneinfo; any bad/unknown value falls through
    to the next candidate so a turn never crashes on a typo. The final fallback
    attaches the host's local offset via astimezone().
    """
    for name in (tz_name, os.getenv("WACTORZ_TZ"), os.getenv("TZ")):
        if not name:
            continue
        try:
            return datetime.now(ZoneInfo(name))
        except Exception:
            logger.debug("Unknown timezone '%s' — trying next candidate", name)
    return datetime.now().astimezone()


def current_time_context(tz_name: str | None = None) -> str:
    """Live date/time preamble to prepend to any agent's system prompt so the LLM
    anchors to the real present moment instead of its training-cutoff guess.
    """
    now = resolve_now(tz_name)
    return (
        "== CURRENT DATE & TIME (live, authoritative — trust over training data) ==\n"
        f"It is now {now.strftime('%A, %d %B %Y, %H:%M')} "
        f"{now.strftime('%Z')} (UTC{now.strftime('%z')}).\n"
        "This is the real present moment. Your training data is older than this; "
        "never infer the current year or date from memory. Resolve relative dates "
        "like 'today', 'tonight', 'tomorrow', 'next Monday' against the time above "
        "and use concrete calendar dates when scheduling.\n"
    )
