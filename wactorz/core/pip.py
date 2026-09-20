"""Installing pip packages on the machine this process runs on.

Two callers share this: the installer agent validates the package names in a
spawn config before sending them anywhere, and a node installs what an agent it
was sent says it imports. Both take their list from a payload off the broker, so
the rule about what a name may look like has to be the same in both places.
"""

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
