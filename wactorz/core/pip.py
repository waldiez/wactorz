"""Installing pip packages on the machine this process runs on.

Two callers share this: the installer agent validates the package names in a
spawn config before sending them anywhere, and a node installs what an agent it
was sent says it imports. Both take their list from a payload off the broker, so
the rule about what a name may look like has to be the same in both places.
"""

import importlib
import importlib.metadata
import os
import re
import sys
from collections.abc import Sequence

#: What a package name may look like. PEP 508 names are letters, digits, and
#: `-`/`_`/`.` as internal separators; a version specifier or extras may follow.
#: Nothing else — no path, no URL, no whitespace, and no leading `-`.
_PACKAGE_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9,._-]+\])?"
    r"(?:"
    r"(?:[=<>!~]=|[<>])[A-Za-z0-9._*+!-]+"
    r"(?:,(?:[=<>!~]=|[<>])[A-Za-z0-9._*+!-]+)*"
    r")?"
)


def is_installable_name(package: str) -> bool:
    """Whether `package` is a package name and not an instruction to pip.

    pip reads its own options from positional arguments, so `--index-url=http://…`
    or a bare URL is honoured as configuration rather than treated as a name.
    That is fetch-and-execute from an attacker-chosen index with no shell
    involved, and it arrives in a spawn config an LLM wrote.

    Sent to a node over SSH the same list is joined into a string, so there the
    value is also shell syntax — `;`, backticks, `$(…)`. `shlex.quote` handles
    that half; this handles the half quoting cannot, because a properly quoted
    `--index-url` is still an option.

    An allow-list rather than a deny-list: the set of legitimate names is small
    and describable, and the set of harmful strings is not.
    """
    candidate = (package or "").strip()
    return bool(candidate) and _PACKAGE_NAME.fullmatch(candidate) is not None


#: How long one `pip install <package>` may run. Generous on purpose: a package
#: that bundles native libraries can take minutes on a fast connection with no
#: pip cache, and far longer on a small board on WiFi. The limit is there to end
#: an install that has hung, not one that is slow.
PIP_INSTALL_TIMEOUT_S = 900.0


def install_wait_s(package_count: int) -> float:
    """How long a caller should wait for the installer to finish `package_count`.

    The installer works through packages one at a time, each bounded by
    `PIP_INSTALL_TIMEOUT_S`, so waiting any less gives up on an install that is
    still inside its own limits, and the caller carries on without it.
    """
    return max(1, package_count) * PIP_INSTALL_TIMEOUT_S + 30.0


#: Where a distribution's module name differs from its pip name. Used by the
#: fallback in `requirement_is_satisfied`; installed metadata answers first and
#: does not care what the module is called.
IMPORT_NAMES = {
    "beautifulsoup4": "bs4",
    "deepgram-sdk": "deepgram",
    "ddgs": "duckduckgo_search",
    "duckduckgo-search": "duckduckgo_search",
    "opencv-python": "cv2",
    "pillow": "PIL",
    "pymupdf": "fitz",
    "pyserial": "serial",
    "python-dateutil": "dateutil",
    "python-docx": "docx",
    "python-pptx": "pptx",
    "pyyaml": "yaml",
    "scikit-image": "skimage",
    "scikit-learn": "sklearn",
    "stable-baselines3": "stable_baselines3",
    "typing-extensions": "typing_extensions",
    "webrtcvad-wheels": "webrtcvad",
}


def requirement_name(requirement: str) -> str:
    """The distribution a requirement names, without extras or a version."""
    name = re.split(r"[\[<>=!~;\s]", (requirement or "").strip(), maxsplit=1)[0]
    return name.lower().replace("_", "-")


def _version_matches(version: str, specifier: str) -> bool:
    """Whether `version` satisfies `specifier` (e.g. ``>=3,<4``)."""
    if not specifier:
        return True
    try:
        # Local: optional dependency — `packaging` is not a Wactorz requirement.
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
    except ImportError:  # pragma: no cover - present wherever pip's usual companions are
        exact = re.fullmatch(r"==\s*([^,;\s]+)", specifier)
        # Without packaging only an exact pin can be judged; anything else is
        # taken as met rather than reinstalled on every spawn.
        return exact is None or exact.group(1) == version
    try:
        return SpecifierSet(specifier).contains(version, prereleases=True)
    except InvalidSpecifier:
        return False


def requirement_is_satisfied(requirement: str) -> bool:
    """Whether `requirement` (``name[extra]>=1,<2``) is already installed.

    Reads installed metadata rather than importing: importing a heavy SDK just
    to ask whether it is there is slow, and a module imported now is the stale
    copy this process keeps if pip replaces it a moment later. The import is
    only a fallback for a module present without metadata.
    """
    name = requirement_name(requirement)
    if not name:
        return False
    specifier = re.sub(r"^[^<>=!~]*", "", (requirement or "").split(";", 1)[0]).strip()
    importlib.invalidate_caches()
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        if specifier:
            return False
        try:
            importlib.import_module(IMPORT_NAMES.get(name, name.replace("-", "_")))
        except ImportError:
            return False
        return True
    return _version_matches(version, specifier)


def installed_versions() -> dict[str, str]:
    """Every installed distribution and its version, keyed by normalised name."""
    versions: dict[str, str] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            versions[requirement_name(name)] = dist.version
    return versions


def stale_loaded_distributions(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Distributions pip changed or removed that this process has already imported.

    Python keeps the modules it has imported. When pip replaces one of those
    distributions on disk, the old copy stays in memory and any submodule
    imported later comes from the new files, so the two halves disagree and the
    import fails in a way nothing but a restart clears. An agent's install that
    downgrades a shared dependency is the usual way this happens. Each entry
    reads ``name old -> new``.
    """
    importlib.invalidate_caches()
    changed = {name for name, version in before.items() if after.get(name) != version}
    if not changed:
        return []
    loaded_roots = {module.split(".", 1)[0] for module in list(sys.modules)}
    stale: set[str] = set()
    for root, dists in importlib.metadata.packages_distributions().items():
        if root not in loaded_roots:
            continue
        for dist in dists:
            name = requirement_name(dist)
            if name in changed:
                stale.add(name)
    return [f"{name} {before[name]} -> {after.get(name, 'removed')}" for name in sorted(stale)]


def missing_requirements(requirements: Sequence[str]) -> list[str]:
    """The entries of `requirements` that are not installed yet, in order."""
    return [req for req in requirements if req.strip() and not requirement_is_satisfied(req)]


def is_root() -> bool:
    """Whether this process may write where the system package manager keeps its files.

    Windows has no uid and the equivalent question is whether the token is
    elevated, which shell32 answers. A failure to reach it is read as "not
    privileged", so the install takes the contained path rather than assuming
    it may write anywhere.
    """
    if os.name == "nt":
        import ctypes  # local: the shell32 call below is the only Windows-only code here

        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0  # pyright: ignore[reportAttributeAccessIssue]
        except Exception:
            return False
    return os.getuid() == 0


def in_virtualenv() -> bool:
    """Whether this interpreter is an environment of its own, not the system one.

    `real_prefix` is what the legacy virtualenv package set; everything since
    moves `prefix` away from `base_prefix`. Both are resolved before comparison,
    because a symlinked environment otherwise looks unequal to itself.
    """
    if hasattr(sys, "real_prefix"):  # pragma: no cover - legacy virtualenv only
        return True
    base = getattr(sys, "base_prefix", sys.prefix)
    return os.path.realpath(base) != os.path.realpath(sys.prefix)


def install_command(packages: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    """A pip command for this machine, and the environment it should run in.

    Ordered by how little of the host it disturbs. Inside a virtualenv nothing
    special is needed and nothing outside it is touched. Outside one, an
    unprivileged install is directed at the user's own site-packages, which
    leaves the distribution's tree alone; only root ends up writing where the
    system package manager expects to be in charge.

    A distribution may refuse either of those under PEP 668, and the override
    goes through the environment rather than `--break-system-packages` because
    no pip before 23.0.1 knows that argument — an edge node running an older one
    would fail on the flag instead of installing. It is passed to the child
    rather than set on this process, so two installs cannot race over it.
    """
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
    ]
    env: dict[str, str] = {}
    if not in_virtualenv():
        if not is_root():
            cmd.append("--user")
        env["PIP_BREAK_SYSTEM_PACKAGES"] = "1"
    cmd += [*packages, "-q"]
    return cmd, env


def install_destination() -> str:
    """Where an install would land, for a log line that says so."""
    if in_virtualenv():
        return f"the virtualenv at {sys.prefix}"
    if is_root():
        return f"the system interpreter at {sys.executable}"
    return "this user's site-packages"
