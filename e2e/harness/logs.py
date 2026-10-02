"""The application log, and the things that must never be in it.

A log is the one artefact that gets copied into a bug report, pasted into a chat
and shipped to whoever is helping. Everything else in this suite asks whether the
system did the right thing; this asks whether it said something it should not
have, which is a claim only a real run can make - the credential has to have been
in the process's environment for its absence from the file to mean anything.

The check is deliberately narrow. It looks for the *values* the run was actually
configured with, not for patterns that resemble secrets: a pattern match invites
either false alarms on an agent's own output or a rule so loose it never fires.
If the broker password appears in the log, that is not a heuristic - the process
was given that string and wrote it down.
"""

from __future__ import annotations

from pathlib import Path

from .run import Run

#: A value shorter than this would match ordinary words in a log.
SHORTEST = 8


def read(path: Path) -> str:
    """The log file's contents, or "" if it was never created.

    Absent is not an error here: a process that failed before logging was
    configured has no log, and the scenario asking about its contents should fail
    on what it actually claims rather than on a missing file.
    """
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def assert_no_secrets(path: Path, run: Run) -> None:
    """Fail if a secret this run made up appears in this log, naming which.

    The failure quotes the line and not the secret, so the report of a leak is
    not a second copy of it.
    """
    contents = read(path)
    leaked: list[str] = []
    for name, value in run.secrets.items():
        if len(value) < SHORTEST:
            continue
        for number, line in enumerate(contents.splitlines(), start=1):
            if value in line:
                redacted = line.replace(value, "<the value>")
                leaked.append(f"{path.name}:{number} contains {name}: {redacted.strip()}")
                break
    if leaked:
        raise AssertionError("credentials reached the log file:\n  " + "\n  ".join(leaked))


def assert_contains(path: Path, needle: str) -> None:
    contents = read(path)
    if needle not in contents:
        raise AssertionError(f"{path} does not mention {needle!r}\n--- log ---\n{contents[-3000:]}")


def assert_absent(path: Path, needle: str) -> None:
    contents = read(path)
    if needle in contents:
        matching = [line for line in contents.splitlines() if needle in line]
        raise AssertionError(
            f"{path} was not supposed to mention {needle!r}:\n  " + "\n  ".join(matching[:10])
        )


def lines_matching(path: Path, needle: str) -> list[str]:
    return [line for line in read(path).splitlines() if needle in line]
