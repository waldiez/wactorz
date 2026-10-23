"""What the operator refused lately, so a model trying again is not asked again.

A model that is told no often tries the same thing once more, sometimes in the
same turn. Asking the operator each time puts the same question on the dashboard
over and over, and an operator who is away answers none of them. So a refusal,
or a question nobody answered, is remembered for a while, and the same request
in that time is refused at once, saying when and how it was refused before.
"""

import time
from collections import OrderedDict
from dataclasses import dataclass

#: How long a refusal stands before the same request is asked about again.
REMEMBERED_S = 600.0

#: How many refusals are remembered. The keys are what a model asked for, so
#: without a bound one that invents names would grow this for as long as the
#: process runs. One forgotten is at worst asked about once more.
MEMORY = 256

#: How a refusal came about, as the caller is told.
REFUSED = "refused"
UNANSWERED = "not answered"


@dataclass(frozen=True)
class Refusal:
    """One remembered refusal: how it came about, and when."""

    how: str
    at: float

    def said(self) -> str:
        """The refusal as a sentence's tail: "refused 3 minutes ago"."""
        minutes = max(0, round((time.time() - self.at) / 60))
        when = "just now" if minutes == 0 else f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        return f"{self.how} {when}"


class RecentRefusals:
    """Refusals by key, each standing for `REMEMBERED_S`."""

    def __init__(self, remembered_s: float = REMEMBERED_S) -> None:
        self._remembered_s = remembered_s
        self._refusals: OrderedDict[str, Refusal] = OrderedDict()

    def remember(self, key: str, how: str) -> None:
        """Remember that ``key`` was refused, ``how`` being `REFUSED` or `UNANSWERED`."""
        self._refusals.pop(key, None)
        self._refusals[key] = Refusal(how, time.time())
        while len(self._refusals) > MEMORY:
            self._refusals.popitem(last=False)

    def forget(self, key: str) -> None:
        """Drop ``key``'s refusal, when the operator has since allowed it."""
        self._refusals.pop(key, None)

    def standing(self, key: str) -> Refusal | None:
        """``key``'s refusal, if it still stands."""
        refusal = self._refusals.get(key)
        if refusal is None:
            return None
        if time.time() - refusal.at >= self._remembered_s:
            del self._refusals[key]
            return None
        return refusal
