"""Where an agent may read a person's documents from, and write what it makes of them.

An agent that converts a file is handed the file's path in a request a model
wrote, so the path is only as trustworthy as everything that model read: an
email, a web page, a Home Assistant entity name. Left open, a crafted request
reads any file the process can (its text then goes to a model) and writes over
any file it can, an agent's pickle included. So such an agent reads and writes
only here:

* **The document folders.** Those ``WACTORZ_DOCUMENT_DIRS`` names, separated
  the way the platform separates ``PATH`` entries; when it is unset, the
  person's Documents folder.
* **Chat uploads, by id.** A file attached in the dashboard is stored under a
  generated id, and the id is all a request names. It is read, never written.

Nothing under the state directory is reachable by path, whatever the folders
say: it holds pickles that are loaded and executed, and the install's keys.

Paths are compared after resolving symlinks, so a link inside a document folder
that points out of it leads nowhere.
"""

import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from .core.paths import resolve_state_dir
from .web.uploads import UPLOADS_DIRNAME, is_id

#: The variable naming the document folders.
DOCUMENT_DIRS_ENV = "WACTORZ_DOCUMENT_DIRS"

#: One line of ``user-dirs.dirs``: ``XDG_DOCUMENTS_DIR="$HOME/Documents"``.
_XDG_DOCUMENTS = re.compile(r'^\s*XDG_DOCUMENTS_DIR\s*=\s*"(?P<path>[^"]*)"\s*$', re.MULTILINE)


class DocumentRefused(ValueError):
    """A path or upload id an agent may not use, with the reason in the message.

    Made by the constructors below, one per reason, so each message is worded
    once and says what to do instead where there is something.
    """

    @classmethod
    def outside(cls, raw: str, roots: list[Path]) -> "DocumentRefused":
        where = ", ".join(str(root) for root in roots)
        return cls(
            f"{raw} is outside the document folders ({where}); copy it there, "
            f"attach it in the chat, or add its folder to {DOCUMENT_DIRS_ENV}"
        )

    @classmethod
    def unusable(cls, raw: str) -> "DocumentRefused":
        return cls(f"{raw!r} is not a usable path")

    @classmethod
    def in_state(cls, raw: str) -> "DocumentRefused":
        return cls(f"{raw} is inside the state directory, which agents may not use")

    @classmethod
    def not_an_id(cls, file_id: str) -> "DocumentRefused":
        return cls(f"{file_id} is not an upload id")

    @classmethod
    def upload_gone(cls, file_id: str) -> "DocumentRefused":
        return cls(f"No upload {file_id}; it may have been removed")

    @classmethod
    def nothing_named(cls) -> "DocumentRefused":
        return cls("Name the file: a path in a document folder, or an upload's id")

    @classmethod
    def not_found(cls, raw: str) -> "DocumentRefused":
        return cls(f"File not found: {raw}")

    @classmethod
    def wrong_kind(cls, raw: str, suffix: str) -> "DocumentRefused":
        return cls(f"{raw} must end in {suffix}")

    @classmethod
    def no_folder(cls, path: Path) -> "DocumentRefused":
        return cls(f"No folder {path.parent} to write {path.name} into")


def default_documents_dir() -> Path:
    """The person's Documents folder, as their desktop names it.

    On Linux the desktop's ``user-dirs.dirs`` can move or translate it, so it
    is read from there; a folder set to the home directory itself is how that
    file says there is none, and is not taken as one. Elsewhere, and failing
    that, ``~/Documents``. Windows can redirect it, to OneDrive for one; such a
    folder is named in ``WACTORZ_DOCUMENT_DIRS``.
    """
    home = Path.home()
    if sys.platform.startswith("linux"):
        config_home = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(home / ".config")
        try:
            text = (Path(config_home) / "user-dirs.dirs").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        found = _XDG_DOCUMENTS.search(text)
        if found:
            named = Path(found["path"].replace("$HOME", str(home)))
            if named.is_absolute() and named != home:
                return named
    return home / "Documents"


def document_dirs() -> list[Path]:
    """The document folders, resolved; read from the environment on every call.

    Read each time rather than once at import, so a node and a test see the
    value they were started with. Empty entries are skipped, and a folder that
    does not exist is kept: it holds nothing yet, and allows nothing more.
    """
    raw = os.environ.get(DOCUMENT_DIRS_ENV, "")
    named = [Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip()]
    return [path.resolve() for path in (named or [default_documents_dir()])]


def _state_dir() -> Path:
    return Path(resolve_state_dir()).resolve()


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _confined(raw: str) -> Path:
    """`raw` resolved, if it is inside a document folder and outside the state directory."""
    roots = document_dirs()
    try:
        path = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as unresolvable:
        # A null byte (ValueError) or, before Python 3.13, a link that leads
        # back to itself (RuntimeError): neither names a file anywhere.
        raise DocumentRefused.unusable(raw) from unresolvable
    if not _inside(path, roots):
        raise DocumentRefused.outside(raw, roots)
    if path.is_relative_to(_state_dir()):
        raise DocumentRefused.in_state(raw)
    return path


def readable_document(file_path: str = "", file_id: str = "") -> Path:
    """The file a request names, by upload id or by path, if an agent may read it.

    Raises :class:`DocumentRefused` naming why not, which goes back to whoever
    asked. An upload is looked up by its id alone, so no part of its path comes
    from the request.
    """
    if file_id:
        if not is_id(file_id):
            raise DocumentRefused.not_an_id(file_id)
        path = _state_dir() / UPLOADS_DIRNAME / file_id
        if not path.is_file():
            raise DocumentRefused.upload_gone(file_id)
        return path
    if not file_path:
        raise DocumentRefused.nothing_named()
    path = _confined(file_path)
    if not path.is_file():
        raise DocumentRefused.not_found(file_path)
    return path


def writable_document(file_path: str, suffix: str) -> Path:
    """Where an agent may write the `suffix` file a request names.

    Only inside a document folder, into a folder that exists, and only a file
    of the kind the agent makes: a request cannot turn a deck into a
    ``.bashrc``, or reach a folder by having it created.
    """
    path = _confined(file_path)
    if path.suffix.lower() != suffix.lower():
        raise DocumentRefused.wrong_kind(file_path, suffix)
    if not path.parent.is_dir():
        raise DocumentRefused.no_folder(path)
    return path
