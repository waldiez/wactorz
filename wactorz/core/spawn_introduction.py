# SPDX-License-Identifier: Apache-2.0
"""Format the introduction at the start of an agent's chat."""

_revision = 0


def introductions_revision() -> int:
    """The process-local version of the introductions a dashboard can read."""
    return _revision


def mark_introductions_changed() -> None:
    """Advance the version after an introduction source changes on the actor loop."""
    global _revision
    _revision += 1


def _short_line(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    line = " ".join(value.split())
    if len(line) > limit:
        return line[: limit - 1].rstrip() + "…"
    return line


def spawn_introduction(description: object, examples: object, *, help_text: str = "") -> str:
    """Say what an agent does and give up to two nonempty examples or a help pointer."""
    about = _short_line(description, 280)
    suggestions = (
        [line for example in examples if (line := _short_line(example, 120))][:2]
        if isinstance(examples, (list, tuple))
        else []
    )
    usage = (
        "Try:\n" + "\n".join(f"- {example}" for example in suggestions)
        if suggestions
        else help_text
    )
    return "\n\n".join(part for part in (about, usage) if part)
