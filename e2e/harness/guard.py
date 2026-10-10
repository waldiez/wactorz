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
command -- says which, with :meth:`Guard.allow` for the backend's console and
:meth:`Guard.allow_on_the_page` for the page's, and everything else still counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .browser import Dashboard

#: What in a log record this suite takes for something having gone wrong.
_WRONG = re.compile(r" \[(ERROR|CRITICAL)\] |Traceback \(most recent call last\)")

#: How a record starts. What follows one without starting so belongs to it: a
#: traceback, or a message of several lines.
_STARTS_A_RECORD = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

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
    _allowed_on_the_page: list[re.Pattern[str]] = field(default_factory=list)

    def begin(self) -> None:
        """Start a scenario: what is already in the console is not its doing."""
        self._read_to = self.console.stat().st_size if self.console.exists() else 0
        self._allowed.clear()
        self._allowed_on_the_page.clear()

    def allow(self, pattern: str) -> None:
        """Expect console records matching ``pattern`` in this scenario, and say why beside it."""
        self._allowed.append(re.compile(pattern))

    def allow_on_the_page(self, pattern: str) -> None:
        """The same, for what the page reports on its own console."""
        self._allowed_on_the_page.append(re.compile(pattern))

    def _console_records(self) -> list[str]:
        """The records written since the scenario began that it did not allow.

        A record is read whole, its traceback with it, so that allowing an
        error a scenario provokes allows what the server logged about it and no
        other traceback.
        """
        if not self.console.exists():
            return []
        with self.console.open(encoding="utf-8", errors="replace") as handle:
            handle.seek(self._read_to)
            new = handle.read()
        records: list[str] = []
        for line in new.splitlines():
            if _STARTS_A_RECORD.match(line):
                records.append(line)
            elif records:
                records[-1] += "\n" + line
            # Else the rest of a record whose first line was written before
            # this scenario began: the scenario before it read that line, and
            # the level it is judged by is there.
        wrong = [record for record in records if _WRONG.search(record)]
        return [record for record in wrong if not any(ok.search(record) for ok in self._allowed)]

    def check(self) -> None:
        """Fail with everything unexpected at once, so one run shows all of it."""
        found: list[str] = []
        found += [
            f"the backend logged: {record.strip()[:600]}" for record in self._console_records()
        ]
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
            found += [
                f"the page reported: {error}"
                for error in self.dashboard.page_errors
                if not any(ok.search(error) for ok in self._allowed_on_the_page)
            ]
            found += [
                f"the dashboard showed an error: {toast}" for toast in self.dashboard.error_toasts
            ]
            self.dashboard.page_errors.clear()
            self.dashboard.error_toasts.clear()
        if found:
            raise Unexpected("during this scenario:\n  " + "\n  ".join(found))
