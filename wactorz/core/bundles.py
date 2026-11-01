"""A library agent's code, as it goes to a node that does not have it.

**The rule: an agent's own code lives in its own folder.** A library agent --
a function declared with ``@wactorz.agent``, an `Actor` subclass -- is built
from a module. When it moves to a node that has no copy of it, main sends the
code with the spawn, and what it sends is that module's folder: the ``.py``
files in it and in the folders under it. For a module inside a package, the
package. Everything else the agent imports is either the standard library,
``wactorz``, or a package that pip installs, listed in the agent's
``requires={"packages": [...]}`` so the node installs it too.

Code the agent imports from anywhere else on this machine -- a package of your
own in another folder, on ``PYTHONPATH`` or installed with ``pip install -e`` --
would not be on the node, and the agent would fail there on an import. So it
is refused here, before anything stops, naming the import: move that code into
the agent's folder, or install it on the node yourself and name the agent in
``WACTORZ_AGENTS`` there.

Only ``.py`` files go. A model is an artifact (see `wactorz.core.artifacts`),
and the state an agent keeps travels with it as state. A folder that is a
virtual environment, a cache or hidden is skipped.

A bundle is a mapping of relative path to source, with the SHA-256 of that
mapping. It travels inside the spawn, which main signs, and the node checks
the hash before it writes a file; it keeps each bundle in a directory named by
it, and imports the agent from there.
"""

import ast
import hashlib
import importlib
import importlib.util
import json
import logging
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

from .pip import refresh_import_caches

logger = logging.getLogger(__name__)

#: The most source a bundle may hold. Far above the agents it is for; a folder
#: over it is almost always one holding something else as well.
BUNDLE_MAX_BYTES = 1 << 20

#: Directories never looked into: caches, tooling and the like.
_SKIPPED_DIRS = frozenset(
    {"__pycache__", "node_modules", "site-packages", "dist-packages", "build", "dist"}
)

#: What a node calls the directory it keeps bundles in, inside its state directory.
BUNDLES_DIRNAME = "..bundles"


class BundleError(Exception):
    """A library agent's code cannot be sent, or what arrived cannot be used."""

    # ── Packing, on main ──────────────────────────────────────────────────

    @classmethod
    def no_module(cls, target: Any) -> "BundleError":
        return cls(f"{target!r} names no module")

    @classmethod
    def not_importable(cls, module_name: str, exc: Exception) -> "BundleError":
        return cls(f"its module {module_name} cannot be imported here: {exc}")

    @classmethod
    def no_files(cls, module_name: str, root: Path) -> "BundleError":
        return cls(f"no Python files were found for {module_name} in {root}")

    @classmethod
    def too_large(cls, folder: Path, size: int) -> "BundleError":
        return cls(
            f"its folder {folder} holds {size} bytes of Python, over "
            f"the {BUNDLE_MAX_BYTES} a bundle may hold"
        )

    @classmethod
    def not_from_a_file(cls, module_name: str) -> "BundleError":
        return cls(f"{module_name} is not read from a file, so it cannot be sent")

    @classmethod
    def imports_outside(cls, named: str) -> "BundleError":
        return cls(
            f"it imports code from outside its folder: {named}. An agent's own code has to "
            "live in its folder to go with it. Move that code into the folder, publish it as "
            "a package and list it in requires, or install it on the node and name the agent "
            "in WACTORZ_AGENTS there"
        )

    @classmethod
    def does_not_parse(cls, relative: str, exc: SyntaxError) -> "BundleError":
        return cls(f"{relative} does not parse: {exc}")

    # ── Checking what arrived, on a node ──────────────────────────────────

    @classmethod
    def not_a_bundle(cls) -> "BundleError":
        return cls("its code is not a bundle")

    @classmethod
    def empty(cls) -> "BundleError":
        return cls("its bundle holds no files")

    @classmethod
    def not_sources(cls) -> "BundleError":
        return cls("its bundle holds something other than source files")

    @classmethod
    def bad_path(cls, relative: str) -> "BundleError":
        return cls(f"its bundle names {relative!r}, which is not a file it may hold")

    @classmethod
    def hash_mismatch(cls) -> "BundleError":
        return cls("its bundle does not match the hash the spawn names")

    @classmethod
    def not_from_spawn(cls, target: str) -> "BundleError":
        return cls(f"{target} was not loaded from the code its spawn carries")

    @classmethod
    def name_taken(cls, name: str, where: str) -> "BundleError":
        return cls(
            f"this node already has a module named {name}, from {where}; give the agent's "
            "module a name of its own"
        )


# ── Main: packing an agent's folder ───────────────────────────────────────────


def bundle_for(target: str) -> dict[str, Any]:
    """The code of the agent at ``target`` (``module:attr``), ready for a spawn.

    Raises `BundleError` when its module cannot be found, when its folder
    imports code from outside it, or when the folder is too large.
    """
    module_name = target.partition(":")[0].strip()
    if not module_name:
        raise BundleError.no_module(target)
    try:
        module = sys.modules.get(module_name) or importlib.import_module(module_name)
    except ImportError as exc:
        raise BundleError.not_importable(module_name, exc) from exc
    root, top = _folder_of(module, module_name)
    files = dict(_sources(root, top))
    if not files:
        raise BundleError.no_files(module_name, root)
    size = sum(len(text.encode("utf-8")) for text in files.values())
    if size > BUNDLE_MAX_BYTES:
        raise BundleError.too_large(root / top if top else root, size)
    _check_imports(files, root)
    return {"sha256": digest(files), "files": files}


def _folder_of(module: ModuleType, module_name: str) -> tuple[Path, str]:
    """Where the agent's code is: the directory it is read from, and the part of it to send.

    A module at the top level -- ``agent`` in ``examples/imu_anomaly/`` -- sends
    its whole folder (``""``). One inside a package sends the package, from
    the directory the package sits in.
    """
    top_name = module_name.split(".")[0]
    top = sys.modules.get(top_name) if top_name != module_name else module
    file = getattr(top, "__file__", None)
    if not file:
        raise BundleError.not_from_a_file(module_name)
    path = Path(file).resolve()
    if hasattr(top, "__path__"):
        # A package: its __init__ is in its own directory.
        return path.parent.parent, path.parent.name
    return path.parent, ""


def _sources(root: Path, top: str) -> Iterator[tuple[str, str]]:
    """Each ``.py`` file to send, as its path relative to ``root`` and its source."""
    start = root / top if top else root
    for path in sorted(_python_files(start)):
        relative = path.relative_to(root).as_posix()
        yield relative, path.read_text(encoding="utf-8")


def _python_files(directory: Path) -> Iterator[Path]:
    for entry in directory.iterdir():
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if _skipped(entry):
                continue
            yield from _python_files(entry)
        elif entry.suffix == ".py":
            yield entry


def _skipped(directory: Path) -> bool:
    return (
        directory.name.startswith(".")
        or directory.name in _SKIPPED_DIRS
        or (directory / "pyvenv.cfg").exists()
    )


def _check_imports(files: Mapping[str, str], root: Path) -> None:
    """Refuse a folder that imports code of this machine's from outside it."""
    local = local_names(files)
    outside: dict[str, str] = {}
    for relative, source in files.items():
        for name in _imported(source, relative):
            if name in local or name in outside:
                continue
            where = _local_elsewhere(name, root)
            if where is not None:
                outside[name] = where
    if outside:
        named = "; ".join(f"{name} (from {where})" for name, where in sorted(outside.items()))
        raise BundleError.imports_outside(named)


def local_names(files: Mapping[str, str]) -> set[str]:
    """The top-level modules and packages a bundle provides."""
    names: set[str] = set()
    for relative in files:
        first = PurePosixPath(relative).parts[0]
        names.add(first.removesuffix(".py"))
    return names


def _imported(source: str, relative: str) -> Iterator[str]:
    """The top-level names ``source`` imports absolutely."""
    try:
        tree = ast.parse(source, filename=relative)
    except SyntaxError as exc:
        raise BundleError.does_not_parse(relative, exc) from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def _local_elsewhere(name: str, root: Path) -> str | None:
    """Where ``name`` is when it is this machine's own code from outside ``root``, else None.

    The standard library, ``wactorz`` and anything installed into an
    environment are not; neither is a name that is not here at all, which an
    agent may import only where it is installed.
    """
    if name in sys.stdlib_module_names or name in {"__future__", "wactorz"}:
        return None
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    places = [spec.origin] if spec.origin else []
    places += list(spec.submodule_search_locations or [])
    paths = [Path(p).resolve() for p in places if p and p not in {"built-in", "frozen"}]
    if not paths or any(_installed(path) for path in paths):
        return None
    if any(path.is_relative_to(root) for path in paths):
        return None
    return str(paths[0])


def _installed(path: Path) -> bool:
    """Whether ``path`` is inside an environment's installed packages or Python's own."""
    if any(part in ("site-packages", "dist-packages") for part in path.parts):
        return True
    prefixes = {Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()}
    return any(path.is_relative_to(prefix) for prefix in prefixes)


def digest(files: Mapping[str, str]) -> str:
    """The SHA-256 a bundle of ``files`` is known by, the same wherever it is worked out."""
    canonical = json.dumps(dict(files), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── A node: taking one in ─────────────────────────────────────────────────────


def checked(bundle: Any) -> dict[str, str]:
    """The files of ``bundle`` as a spawn carried it, once each is safe to write.

    Raises `BundleError` for a bundle that is not well formed, names a path
    outside its directory or anything but a ``.py`` file, or does not hash to
    the SHA-256 it names.
    """
    if not isinstance(bundle, dict):
        raise BundleError.not_a_bundle()
    files = bundle.get("files")
    if not isinstance(files, dict) or not files:
        raise BundleError.empty()
    for relative, source in files.items():
        if not isinstance(relative, str) or not isinstance(source, str):
            raise BundleError.not_sources()
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative or path.suffix != ".py":
            raise BundleError.bad_path(relative)
    if digest(files) != bundle.get("sha256"):
        raise BundleError.hash_mismatch()
    return dict(files)


def write(bundle: dict[str, Any], bundles_dir: Path) -> Path:
    """Write a checked bundle under ``bundles_dir``, once; the directory it is in.

    Written to a directory beside it and renamed into place, so a node that
    stops part way never imports half a bundle.
    """
    files = checked(bundle)
    target = bundles_dir / str(bundle["sha256"])
    if target.is_dir():
        return target
    staging = bundles_dir / f".{bundle['sha256']}.part"
    if staging.exists():
        _remove(staging)
    for relative, source in files.items():
        path = staging.joinpath(*PurePosixPath(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    staging.replace(target)
    return target


def _remove(directory: Path) -> None:
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    directory.rmdir()


def prepare_import(directory: Path, bundles_dir: Path, in_use: set[Path]) -> None:
    """Make the bundle in ``directory`` what its module names import, in this process.

    Modules of the same names loaded from an older bundle no running agent
    uses are forgotten, so a changed agent runs its new code. One loaded from
    anywhere else -- a bundle another running agent uses, or a package
    installed on the node -- is refused rather than replaced under that agent:
    two agents here cannot both own a module called ``model``.
    """
    directory = directory.resolve()
    bundles_dir = bundles_dir.resolve()
    for name in sorted(local_names_in(directory)):
        for loaded in [n for n in sys.modules if n == name or n.startswith(f"{name}.")]:
            owner = _loaded_from(sys.modules[loaded], bundles_dir)
            if owner == directory:
                continue
            if owner is not None and owner not in in_use:
                del sys.modules[loaded]
                continue
            where = "another agent's code" if owner is not None else "a package installed here"
            raise BundleError.name_taken(name, where)
    kept = [p for p in sys.path if not _stale_entry(p, bundles_dir, in_use | {directory})]
    sys.path[:] = [str(directory), *(p for p in kept if p != str(directory))]
    refresh_import_caches()


def local_names_in(directory: Path) -> set[str]:
    """The top-level modules and packages the bundle written in ``directory`` provides."""
    return {
        entry.name.removesuffix(".py")
        for entry in directory.iterdir()
        if entry.suffix == ".py" or (entry.is_dir() and not entry.name.startswith("."))
    }


def _loaded_from(module: ModuleType, bundles_dir: Path) -> Path | None:
    """The bundle directory ``module`` was loaded from, or None when it was not one."""
    file = getattr(module, "__file__", None)
    if not file:
        return None
    path = Path(file).resolve()
    if not path.is_relative_to(bundles_dir):
        return None
    return bundles_dir / path.relative_to(bundles_dir).parts[0]


def _stale_entry(entry: str, bundles_dir: Path, in_use: set[Path]) -> bool:
    """Whether the ``sys.path`` entry ``entry`` is a bundle no running agent uses."""
    try:
        path = Path(entry).resolve()
    except (OSError, ValueError):
        return False
    return path.is_relative_to(bundles_dir) and path not in in_use


def trim(bundles_dir: Path, keep: set[Path]) -> None:
    """Remove every bundle under ``bundles_dir`` but those in ``keep``, as far as it can.

    A bundle no agent here runs is not needed again: one that comes back
    arrives with its spawn and is written anew.
    """
    if not bundles_dir.is_dir():
        return
    for entry in bundles_dir.iterdir():
        if entry.is_dir() and entry.resolve() not in keep and not entry.name.startswith("."):
            try:
                _remove(entry)
            except OSError:
                logger.debug("Left the bundle %s in place", entry)
