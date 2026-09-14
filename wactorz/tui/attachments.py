"""Files attached to a chat turn from the terminal.

A terminal has no drop event. Dropping a file onto one pastes its path, quoted
or escaped the way a shell would want it, and some terminals paste a
``file://`` URI instead. So a drop is recognised from the pasted text: every
piece of it has to be an absolute path or a file URI, and every one has to name
a file that exists. Anything else is an ordinary paste. A relative word such as
``README.md`` never counts, because that is what typing looks like.

Which files are accepted, and how large, follows the dashboard, so a file the
browser would refuse is refused here too.
"""

from __future__ import annotations

import mimetypes
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .. import config
from ..web import uploads

#: MIME-type prefixes the dashboard accepts: ``ACCEPTED_MIME`` in
#: ``frontend/src/ui/dashboard/uploads.ts``.
ACCEPTED_MIME = ("image/", "audio/", "text/", "application/pdf")

#: Extensions accepted when the guessed type matches no prefix: the dashboard's
#: ``ACCEPTED_EXT``.
ACCEPTED_EXT = (
    ".pdf",
    ".txt",
    ".md",
    ".csv",
    ".ppt",
    ".pptx",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".json",
)

#: A drive letter after the leading slash of a ``file:///C:/...`` URI's path.
_WINDOWS_DRIVE = re.compile(r"^/[A-Za-z]:")


@dataclass(frozen=True)
class Staged:
    """A dropped file waiting to go out with the next message."""

    path: Path
    name: str
    size: int


def paths_in_paste(text: str) -> list[Path] | None:
    """The paths a paste names, or None when it reads as ordinary text.

    Pure: nothing here touches the disk, so it can run inside the paste event.
    Whether the paths exist is :func:`examine`'s question.
    """
    tokens: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            tokens.extend(shlex.split(line, posix=os.name != "nt"))
        except ValueError:
            # An unbalanced quote: prose, not a path a terminal produced.
            return None
    found = [path for path in map(_as_path, tokens) if path is not None]
    if not found or len(found) != len(tokens):
        return None
    return found


def _as_path(token: str) -> Path | None:
    """``token`` as an absolute path, or None if it is not one."""
    if os.name == "nt":
        token = token.strip('"')
    if token.startswith("file://"):
        token = unquote(urlparse(token).path)
        if _WINDOWS_DRIVE.match(token):
            token = token[1:]
    path = Path(token).expanduser()
    return path if path.is_absolute() else None


def examine(paths: list[Path]) -> tuple[list[Staged], list[str]] | None:
    """Split dropped files into ones to attach and why the rest were skipped.

    None when any of them is not an existing file: then the paste was a path
    someone copied, and it belongs in the command bar as text. Reads the disk,
    so call it off the event loop.
    """
    accepted: list[Staged] = []
    skipped: list[str] = []
    for path in paths:
        try:
            if not path.is_file():
                return None
            size = path.stat().st_size
        except OSError:
            return None
        reason = refusal(path, size)
        if reason:
            skipped.append(f"{path.name} ({reason})")
        else:
            accepted.append(Staged(path=path, name=path.name, size=size))
    return accepted, skipped


def refusal(path: Path, size: int) -> str:
    """Why the dashboard would not take this file, or ``""`` when it would."""
    if not accepted_type(path):
        return "unsupported type"
    if size > config.UPLOAD_MAX_BYTES:
        return f"over {human_size(config.UPLOAD_MAX_BYTES)}"
    if size == 0:
        return "empty"
    return ""


def accepted_type(path: Path) -> bool:
    """Whether the dashboard's accept-list covers this file, by type or extension."""
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith(ACCEPTED_MIME):
        return True
    return path.suffix.lower() in ACCEPTED_EXT


def store_staged(staged: Staged) -> dict[str, Any]:
    """Store a staged file the way the upload endpoint does. Reads the disk."""
    return dict(uploads.store(staged.path.read_bytes(), staged.name))


def human_size(size: float) -> str:
    """A byte count as the dashboard shows it: B, KB or MB."""
    if size < 1024:
        return f"{int(size)} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def describe(item: Mapping[str, Any]) -> str:
    """One attachment as the tray and the transcript show it.

    Takes a stored record or a chat_log row's entry, so it trusts neither: a
    size that is not a positive number is simply left out.
    """
    name = str(item.get("name") or "attachment")
    size = item.get("size")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
        return f"📎 {name} ({human_size(size)})"
    return f"📎 {name}"
