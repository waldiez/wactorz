"""A wheel of a package as it is installed here, for a machine that must run the same code.

A node runs the package the server runs. Where the server was installed from a
package index, the node installs the same version from there. Where it was
installed from somewhere else -- a git reference, as a Home Assistant add-on
built from a branch is, or a local file -- the index either has no such version
or has different code under the same number, and there is no checkout to build
from. What there is, is the installed package itself: the files, and a record
of every one of them. A wheel is those files in an archive, so one can be put
together again from an install, and handed to the node.

Only for a package that is pure Python and was installed from a direct
reference. One that came from an index is left to the index.
"""

import base64
import hashlib
import importlib.metadata
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

#: What an installer writes into the package's metadata about the install
#: itself, and a wheel does not carry. The record of files is written anew.
_OF_THE_INSTALL = {"INSTALLER", "REQUESTED", "direct_url.json", "RECORD"}

_TAG = re.compile(r"^Tag:\s*(\S+)\s*$", re.MULTILINE)


def installed_from_a_direct_reference(name: str) -> bool:
    """Whether ``name`` was installed from a URL, a repository or a path, not an index.

    Not an editable install: that one is a pointer to a source tree, with no
    files of its own to make a wheel of, and the source tree is what to build.
    """
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    recorded = distribution.read_text("direct_url.json")
    if recorded is None:
        return False
    try:
        origin = json.loads(recorded)
    except ValueError:
        return False
    return not (isinstance(origin, dict) and (origin.get("dir_info") or {}).get("editable"))


def wheel_of_installed(name: str, directory: Path) -> Path | None:
    """Write a wheel of the installed ``name`` into ``directory``. None if it cannot be one.

    It cannot when the package is not installed, keeps no record of its files,
    or is built for one platform: a wheel put together on this machine would
    carry this machine's compiled files to another.
    """
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    files = distribution.files
    tags = _TAG.findall(distribution.read_text("WHEEL") or "")
    if not files or len(tags) != 1 or not tags[0].endswith("-none-any"):
        return None

    kept = [PurePosixPath(str(file)) for file in files if _belongs_in_a_wheel(str(file))]
    metadata = next((path.parts[0] for path in kept if path.parts[0].endswith(".dist-info")), None)
    if metadata is None:
        return None

    directory.mkdir(parents=True, exist_ok=True)
    wheel = directory / f"{metadata.removesuffix('.dist-info')}-{tags[0]}.whl"
    record: list[str] = []
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(kept):
            data = Path(str(distribution.locate_file(str(path)))).read_bytes()
            archive.writestr(str(path), data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
            record.append(f"{path},sha256={digest.decode('ascii')},{len(data)}")
        record.append(f"{metadata}/RECORD,,")
        archive.writestr(f"{metadata}/RECORD", "\n".join(record) + "\n")
    return wheel


def _belongs_in_a_wheel(recorded: str) -> bool:
    """Whether a file the install recorded is one the package itself brought.

    Not the commands an installer generated beside the environment, which are
    recorded as paths leading out of it and made again from the entry points;
    not compiled caches; and not the installer's own notes.
    """
    path = PurePosixPath(recorded)
    if ".." in path.parts or path.suffix == ".pyc" or "__pycache__" in path.parts:
        return False
    in_metadata = len(path.parts) == 2 and path.parts[0].endswith(".dist-info")
    return not (in_metadata and path.name in _OF_THE_INSTALL)
