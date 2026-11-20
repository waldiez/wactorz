# SPDX-License-Identifier: Apache-2.0
"""Install the node runner as a systemd service, at the least-privileged rung.

The deploy used to launch the runner with `nohup`, which does not survive a
reboot and is not restarted when it crashes. This installs a unit instead,
choosing the weakest privilege the node actually supports and reporting which
one it got: a node that quietly fell back to `nohup` is otherwise
indistinguishable from a supervised one.

The ladder is root, then a unit set up beforehand by `wactorz-node
setup-local`, then a user unit, then passwordless sudo, then `nohup`. A user
unit sits ahead of sudo deliberately — it needs no privilege at all — but it
only counts when lingering is on, because without it the unit dies with the
last session and fails the very thing this module is for.

The unit set up beforehand is the local node's: a sandboxed system unit for an
unprivileged user, written once by root (see :mod:`wactorz.node.local_setup`).
A deploy may restart it and read its log, nothing more, so the sandbox is root's
to change, never the deploy's -- and never the code running inside it.

Commands run through an injected callable rather than an SSH connection, so the
ladder is testable without a node.
"""

import logging
import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

logger: logging.Logger = logging.getLogger(__name__)

UNIT_NAME = "wactorz-node.service"

#: How long systemd waits to hear from a node before it restarts it, in seconds.
WATCHDOG_S = 300
SYSTEM_UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"

#: `systemctl --user` talks to the user manager over a bus named by
#: XDG_RUNTIME_DIR, and an SSH exec channel is non-interactive, so the variable
#: is usually absent there even when the user manager is running. Without this
#: prefix the probe reports "Failed to connect to bus", which reads as "no user
#: systemd" and drops us to the sudo rung — escalating privilege on a node where
#: the unprivileged path was available all along.
USER_ENV = "export XDG_RUNTIME_DIR=/run/user/$(id -u); "

Runner = Callable[[str], Awaitable[tuple[bool, str]]]


@dataclass(frozen=True)
class Rung:
    """One step of the ladder: how the runner ends up supervised, if it does."""

    name: str
    label: str
    system: bool


ROOT = Rung("root", "systemd (system)", True)
MANAGED = Rung("managed", "systemd (system, sandboxed, set up by setup-local)", True)
USER = Rung("user", "systemd (user)", False)
SUDO = Rung("sudo", "systemd (system, sudo)", True)
NOHUP = Rung("nohup", "nohup — unsupervised", False)


def unit_file(home: str, user: str, *, system: bool) -> str:
    """The unit text for one rung.

    Every value the runner needs comes from the EnvironmentFile, so the unit is
    identical across nodes and carries no node name. That matters: a node name
    may legally contain a space, a `;` or a `$(…)` — `deploy_name_error` forbids
    only the MQTT wildcards — and systemd substitutes `${VAR}` as a single
    argument without word splitting, so the name survives intact where a shell
    command line would have needed escaping.
    """
    exec_start = (
        f"{home}/wactorz/venv/bin/wactorz-node "
        "--mqtt-broker ${WACTORZ_BROKER} --mqtt-port ${WACTORZ_PORT} --node ${WACTORZ_NODE}"
    )
    lines = [
        "[Unit]",
        "Description=Wactorz remote node",
        "After=network-online.target",
        "Wants=network-online.target",
        # StartLimit* belong to [Unit] on systemd 229 and later, not [Service].
        "StartLimitIntervalSec=120",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
    ]
    if system:
        lines.append(f"User={user}")
    lines += [
        f"WorkingDirectory={home}/wactorz",
        f"EnvironmentFile={home}/wactorz/.env",
        f"ExecStart={exec_start}",
        # on-failure, not always: `/nodes shutdown` exits 0 on purpose, and
        # `always` would turn that command into a restart.
        "Restart=on-failure",
        "RestartSec=5",
        # The node tells systemd its event loop is running, and is restarted
        # when it stops saying so: a node frozen in blocking code is otherwise
        # a process that exists and does nothing, which no restart policy sees.
        # Minutes rather than seconds, since a slow board under load must not
        # be mistaken for a frozen one.
        f"WatchdogSec={WATCHDOG_S}",
        "NotifyAccess=main",
        # Killed outright. systemd's default is to abort the process, which
        # writes a core dump of it each time, onto storage a node has little
        # of; and a frozen event loop would not act on a politer signal.
        "WatchdogSignal=SIGKILL",
        # Exit 2 is a node name containing an MQTT wildcard, which can never
        # succeed on retry. It is also argparse's error code, so a malformed
        # ExecStart fails once instead of hammering.
        "RestartPreventExitStatus=2",
        "",
        "[Install]",
        f"WantedBy={'multi-user.target' if system else 'default.target'}",
        "",
    ]
    return "\n".join(lines)


#: The first line of a unit `wactorz-node setup-local` wrote, which is how a
#: deploy knows to restart it rather than write its own.
MANAGED_MARKER = (
    "# Set up by `wactorz-node setup-local`. A deploy restarts this unit, never rewrites it."
)


def sandboxed_unit_file(home: str, user: str, hidden: Sequence[str] = ()) -> str:
    """The local node's system unit: ``unit_file``'s, inside a sandbox.

    The code this node runs is written by a model, possibly from text someone
    else wrote, so the unit holds it to the node's own state directory: the rest
    of the file system read-only, homes and ``hidden`` (main's settings and
    state) not there at all, no devices, no capabilities, no way to gain
    privilege, other users' processes invisible. The venv is read-only too, so
    code here cannot change what the next start runs.

    Libraries that cache under ``~/.cache`` are pointed into the state
    directory, the one place they can write.
    """
    body = unit_file(home, user, system=True)
    state = f"{home}/wactorz/state"
    sandbox = [
        "# Model-written code runs here: the sandbox is the point of this node.",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        # Strict is what holds the node's own home read-only: it is under
        # /var/lib, not /home, so ProtectHome does not reach it. `full` would
        # leave /var writable, home and venv with it.
        "ProtectSystem=strict",
        "ProtectHome=yes",
        f"ReadWritePaths={_unit_path_value(state)}",
        *(f"InaccessiblePaths=-{_unit_path_value(path)}" for path in hidden),
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectClock=yes",
        "ProtectHostname=yes",
        "ProtectProc=invisible",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "SystemCallArchitectures=native",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
        "UMask=0077",
        f"Environment=XDG_CACHE_HOME={_unit_path_value(state)}/.cache",
        "",
    ]
    head, install = body.split("[Install]", 1)
    return (
        MANAGED_MARKER
        + "\n"
        + head.rstrip("\n")
        + "\n"
        + "\n".join(sandbox)
        + "\n[Install]"
        + install
    )


def _unit_path_value(path: str) -> str:
    """``path`` as a unit file value: ``%`` starts a specifier there, so it is doubled.

    Whitespace and quotes would split or end the value; the caller refuses such
    paths before they reach a unit (see :func:`wactorz.node.local_setup.unit_safe`).
    """
    return path.replace("%", "%%")


def user_unit_path(home: str) -> str:
    """Where the runner's user unit file goes, under ``home``."""
    return f"{home}/.config/systemd/user/{UNIT_NAME}"


def unit_path(rung: Rung, home: str) -> str:
    """Where the runner's unit file goes on ``rung``: the system path, or the user's under
    ``home``.
    """
    return SYSTEM_UNIT_PATH if rung.system else user_unit_path(home)


def systemctl(rung: Rung) -> str:
    """The `systemctl` invocation that reaches this rung's manager."""
    if rung is USER:
        return f"{USER_ENV}systemctl --user"
    if rung is SUDO or rung is MANAGED:
        return "sudo -n systemctl"
    return "systemctl"


#: How many of the node's last log lines a failed deploy shows.
LOG_TAIL_LINES = 12

_TAIL_OPTIONS = f"-n {LOG_TAIL_LINES} --no-pager -o cat"

#: The read of the system unit's journal a failed deploy makes. `wactorz-node
#: setup-local` writes a sudoers rule for exactly this command, arguments and
#: all, since sudo matches them word for word: change one, and the other with it.
JOURNAL_TAIL = f"journalctl -u {UNIT_NAME} {_TAIL_OPTIONS}"

#: The same read for the user unit. `--user-unit=` rather than `--user -u`: the
#: latter reads only a journal kept per user, which many nodes do not keep, and
#: the former reads that one and the system journal both.
USER_JOURNAL_TAIL = f"journalctl --user-unit={UNIT_NAME} {_TAIL_OPTIONS}"

#: Who may read the system journal without sudo.
_JOURNAL_READERS = "root and the adm and systemd-journal groups"

#: A journal read as root, or through sudo, that came back empty: there is
#: nothing it was kept from.
_JOURNAL_EMPTY = f"The node's journal holds no lines from {UNIT_NAME}."

#: A journal read as root that failed: nothing was kept from it, so the journal
#: itself is the problem, and the command is named for whoever runs it by hand.
_JOURNAL_FAILED_AS_ROOT = f"The node's journal could not be read: `{JOURNAL_TAIL}` failed as root."


@dataclass(frozen=True)
class LogTail:
    """The node's last log lines, and what to say instead when there are none."""

    lines: str
    why_empty: str


async def log_tail(run: Runner, rung: Rung, *, node_name: str, user: str) -> LogTail:
    """The last lines the node's runner logged, read the way ``rung`` runs it.

    The system journal is readable only to root and two groups, and the deploy
    user is often in neither: the node's own account on the managed rung never
    is. So the system rungs below root read it through sudo, and when sudo
    refuses, without it, which still works for a user in one of those groups.
    ``why_empty`` says which of those happened, so an empty tail is never just
    silence.
    """
    if rung is NOHUP:
        log = f"~/wactorz/{shlex.quote(node_name + '.log')}"
        ok, lines = await run(f"tail -n {LOG_TAIL_LINES} {log} 2>/dev/null")
        said = "is empty" if ok else "is missing, or could not be read"
        return LogTail(lines, f"The node's log file, {log}, {said}.")
    if rung is USER:
        ok, lines = await run(f"{USER_JOURNAL_TAIL} 2>/dev/null")
        return LogTail(lines, _user_journal_empty(user, read=ok))
    if rung is ROOT:
        ok, lines = await run(f"{JOURNAL_TAIL} 2>/dev/null")
        return LogTail(lines, _JOURNAL_EMPTY if ok else _JOURNAL_FAILED_AS_ROOT)
    ok, lines = await run(f"sudo -n {JOURNAL_TAIL} 2>/dev/null")
    if ok:
        return LogTail(lines, _JOURNAL_EMPTY)
    _, lines = await run(f"{JOURNAL_TAIL} 2>/dev/null")
    return LogTail(lines, _sudo_refused_journal(rung, user))


def _user_journal_empty(user: str, *, read: bool) -> str:
    """Why a user unit's journal gave nothing: possibly not kept where its user can read.

    A read that failed opened no journal at all; one that succeeded may still
    have opened only the user's own, with the unit's lines in the system one.
    The likely cause and its fix are the same either way.
    """
    said = f"gave {user} no lines from its user unit" if read else f"could not be read as {user}"
    return (
        f"The node's journal {said}. Where the journal is not kept per "
        f"user, the unit's lines are in the system journal, readable only to "
        f"{_JOURNAL_READERS}: add {user} to systemd-journal to see them."
    )


def _sudo_refused_journal(rung: Rung, user: str) -> str:
    """Why a system journal read below root gave nothing: sudo would not run it."""
    said = f"The node's journal gave {user} no lines: sudo does not let it run `{JOURNAL_TAIL}`"
    if rung is MANAGED:
        return (
            f"{said}. Run `sudo wactorz-node setup-local` on the node again: the sudoers "
            "rule it writes allows that read."
        )
    return (
        f"{said}, and without sudo the system journal is readable only to {_JOURNAL_READERS}. "
        f"Allow that command in {user}'s sudoers rule, or add {user} to systemd-journal."
    )


async def linger_enabled(run: Runner) -> bool:
    """Whether user services outlive the session — enabling it if we may.

    Read, try, read again. `enable-linger` is gated by polkit's
    `set-self-linger`, which is granted to active sessions, and whether an SSH
    exec channel counts as one varies by distro — so the answer is measured
    rather than predicted. `--no-ask-password` keeps an `auth_admin_keep` policy
    from waiting on a prompt this channel can never answer.

    Enabling writes `/var/lib/systemd/linger/$USER`, which is state outside
    `~/wactorz`, so it is logged rather than done quietly. It is not undone on
    teardown: we may not have been the one who set it.
    """
    read = 'loginctl show-user "$USER" --property=Linger'
    if "Linger=yes" in (await run(read))[1]:
        return True
    await run("loginctl enable-linger --no-ask-password")
    enabled = "Linger=yes" in (await run(read))[1]
    if enabled:
        logger.info("Enabled systemd lingering on the node so a user unit outlives the session")
    return enabled


async def choose_rung(run: Runner) -> Rung:
    """The weakest rung this node can actually hold."""
    if (await run("id -u"))[1].strip() == "0":
        return ROOT
    if await managed_unit_here(run):
        return MANAGED
    user_manager, _ = await run(f"{USER_ENV}systemctl --user show --property=Version")
    if user_manager and await linger_enabled(run):
        return USER
    if (await run("sudo -n true"))[0]:
        return SUDO
    return NOHUP


async def managed_unit_here(run: Runner) -> bool:
    """Whether root set this node's unit up beforehand, and lets this user restart it.

    Both, read from the node: the marker on the unit's first line, and sudo
    saying -- without asking for a password -- that the restart is allowed.

    A marked unit sudo will not restart is said at warning: the deploy then
    takes a weaker rung, and without the line that would look like the setup
    had been undone. The usual cause is the sudoers rule naming a different
    ``systemctl`` from the one sudo's ``secure_path`` finds, on a system whose
    ``/bin`` and ``/usr/bin`` are not merged.
    """
    marked = f"head -n 1 {SYSTEM_UNIT_PATH} 2>/dev/null | grep -qxF {shlex.quote(MANAGED_MARKER)}"
    if not (await run(marked))[0]:
        return False
    if (await run(f"sudo -n -l systemctl restart {UNIT_NAME} >/dev/null 2>&1"))[0]:
        return True
    logger.warning(
        "This node's unit was set up by setup-local, but sudo will not let this user restart "
        "it, so the deploy installs a unit of its own. Check the rule in /etc/sudoers.d/"
        "wactorz-node names the systemctl that `sudo -n -l systemctl` finds, and run "
        "setup-local again."
    )
    return False


async def install(run: Runner, *, user: str, home: str) -> Rung:
    """Install and enable the unit, returning the rung actually reached.

    `NOHUP` is returned both when no rung is available and when a chosen one
    fails to write or enable — `sudo -n true` proves only that some passwordless
    sudo exists, and NOPASSWD can be scoped to commands that do not include
    `tee` or `systemctl`. Either way the caller launches the runner itself and
    reports that it is unsupervised.
    """
    rung = await choose_rung(run)
    if rung is not NOHUP and await _write_and_enable(run, rung, user=user, home=home):
        await _remove_units(run, home=home, keep=rung)
        return rung
    # Nothing was installed, so every unit here is stale — and that is the
    # dangerous case rather than the tidy one. A node that had a unit and now
    # probes to nohup (lingering switched off, sudo revoked, a scoped NOPASSWD
    # that refuses `tee`) would otherwise keep it enabled: the caller's `pkill`
    # stops the runner, the unit restarts it under Restart=on-failure, and the
    # nohup launch adds a second one answering the same control topics.
    await _remove_units(run, home=home, keep=None)
    return NOHUP


async def _write_and_enable(run: Runner, rung: Rung, *, user: str, home: str) -> bool:
    if rung is MANAGED:
        # Written and enabled by root already; this user may only restart it.
        return (await run(f"sudo -n systemctl restart {UNIT_NAME}"))[0]
    path = unit_path(rung, home)
    directory = path.rsplit("/", 1)[0]
    body = unit_file(home, user, system=rung.system)
    # A quoted heredoc: the unit is full of ${WACTORZ_*} that systemd expands
    # and the shell must not.
    if rung is SUDO:
        write = f"sudo -n mkdir -p {directory} && sudo -n tee {path} > /dev/null <<'WZUNIT'\n{body}WZUNIT"
    else:
        write = f"mkdir -p {directory} && cat > {path} <<'WZUNIT'\n{body}WZUNIT"
    if not (await run(write))[0]:
        return False
    control = systemctl(rung)
    if not (await run(f"{control} daemon-reload"))[0]:
        return False
    if not (await run(f"{control} enable {UNIT_NAME}"))[0]:
        return False
    # `restart`, not `enable --now`. The caller has just killed the old runner
    # with SIGTERM, whose handler exits 0 -- a clean exit, which Restart=on-failure
    # rightly declines to bring back -- and `--now` does not start a unit that is
    # still deactivating, so the node stays dead. `restart` is also the only verb
    # that adopts freshly uploaded code when the unit was left running: `--now`
    # treats an already-active unit as nothing to do.
    return (await run(f"{control} restart {UNIT_NAME}"))[0]


async def _remove_units(run: Runner, *, home: str, keep: Rung | None) -> None:
    """Drop every unit except the one just installed; `keep=None` drops them all.

    The ladder is re-probed on every deploy, so a node whose privileges changed
    between two of them installs at the new location while the old unit stays
    enabled at the old one. `pkill` clears the running process; after a reboot
    both units would start a runner, which is the two-runners-one-name failure
    the pkill exists to prevent.

    Best effort by necessity, and unconditionally so: removing a system unit
    from the user rung needs a privilege that rung does not have. The system
    removal is attempted with and without `sudo` because the fallback path does
    not know which one it lacked.
    """
    if keep is not USER:
        stale = user_unit_path(home)
        await run(
            f"{USER_ENV}systemctl --user disable --now {UNIT_NAME} 2>/dev/null; rm -f {stale}; true"
        )
    if keep is None or not keep.system:
        await run(
            f"systemctl disable --now {UNIT_NAME} 2>/dev/null; "
            f"rm -f {SYSTEM_UNIT_PATH} 2>/dev/null; "
            f"sudo -n systemctl disable --now {UNIT_NAME} 2>/dev/null; "
            f"sudo -n rm -f {SYSTEM_UNIT_PATH} 2>/dev/null; true"
        )
