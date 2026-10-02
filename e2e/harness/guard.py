"""What must not have happened while a scenario ran, whatever it was checking.

A scenario asserts the things it is about. This asserts the rest, after every
one of them: that nothing was said, logged or shown that nobody asked for. It is
the difference between "the deploy finished" and "the deploy finished, and the
server did not log a traceback on the way".

Four places are read:

- the chat thread, for an agent message no scenario claimed;
- the backend's console, for a line at ERROR or above, or a traceback;
- the page, for an error on its console or an exception nothing caught;
- the dashboard's toasts, for one of the error kind.

A scenario that provokes an error on purpose -- the broker taken away, a refused
command -- says which, with :meth:`Guard.allow`, and everything else still counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .browser import Dashboard

#: A console line this suite takes for something having gone wrong.
_WRONG = re.compile(r" \[(ERROR|CRITICAL)\] |Traceback \(most recent call last\)")

#: What the scripted model says when a scenario did not script the question. An
#: agent that says it was asked something the scenario did not plan for.
UNSCRIPTED = "This is a scripted reply from the fake LLM provider."


class Unexpected(AssertionError):
    """Something happened during a scenario that it did not ask for."""


@dataclass
class Guard:
    """Reads, at the end of a scenario, everything that happened during it."""

    console: Path
    dashboard: Dashboard | None = None
    _read_to: int = 0
    _allowed: list[re.Pattern[str]] = field(default_factory=list)

    def begin(self) -> None:
        """Start a scenario: what is already in the console is not its doing."""
        self._read_to = self.console.stat().st_size if self.console.exists() else 0
        self._allowed.clear()

    def allow(self, pattern: str) -> None:
        """Expect console lines matching ``pattern`` in this scenario, and say why beside it."""
        self._allowed.append(re.compile(pattern))

    def _console_lines(self) -> list[str]:
        if not self.console.exists():
            return []
        with self.console.open(encoding="utf-8", errors="replace") as handle:
            handle.seek(self._read_to)
            new = handle.read()
        wrong = [line for line in new.splitlines() if _WRONG.search(line)]
        return [line for line in wrong if not any(ok.search(line) for ok in self._allowed)]

    def check(self) -> None:
        """Fail with everything unexpected at once, so one run shows all of it."""
        found: list[str] = []
        found += [f"the backend logged: {line.strip()[:400]}" for line in self._console_lines()]
        if self.dashboard is not None:
            self.dashboard.note_toasts()
            found += [
                f"in the thread with {agent}, said and not expected: {said}"
                for agent, pending in self.dashboard.all_unclaimed().items()
                for said in pending
            ]
            found += [
                f"{said.sender} answered a question the model has no script for"
                for said in self.dashboard.all_said()
                if UNSCRIPTED in said.text
            ]
            found += [f"the page reported: {error}" for error in self.dashboard.page_errors]
            found += [
                f"the dashboard showed an error: {toast}" for toast in self.dashboard.error_toasts
            ]
            self.dashboard.page_errors.clear()
            self.dashboard.error_toasts.clear()
        if found:
            raise Unexpected("during this scenario:\n  " + "\n  ".join(found))
