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
A deploy may restart it and nothing more, so the sandbox is root's to change,
never the deploy's -- and never the code running inside it.

Commands run through an injected callable rather than an SSH connection, so the
ladder is testable without a node.
"""

import logging
import shlex
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

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
    return f"{home}/.config/systemd/user/{UNIT_NAME}"


def unit_path(rung: Rung, home: str) -> str:
    return SYSTEM_UNIT_PATH if rung.system else user_unit_path(home)


def systemctl(rung: Rung) -> str:
    """The `systemctl` invocation that reaches this rung's manager."""
    if rung is USER:
        return f"{USER_ENV}systemctl --user"
    if rung is SUDO or rung is MANAGED:
        return "sudo -n systemctl"
    return "systemctl"


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
