"""Install the remote runner as a systemd service, at the least-privileged rung.

The deploy used to launch `remote_runner.py` with `nohup`, which does not
survive a reboot and is not restarted when it crashes. This installs a unit
instead, choosing the weakest privilege the node actually supports and
reporting which one it got: a node that quietly fell back to `nohup` is
otherwise indistinguishable from a supervised one.

The ladder is root, then a user unit, then passwordless sudo, then `nohup`.
A user unit sits ahead of sudo deliberately — it needs no privilege at all —
but it only counts when lingering is on, because without it the unit dies with
the last session and fails the very thing this module is for.

Commands run through an injected callable rather than an SSH connection, so the
ladder is testable without a node.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

UNIT_NAME = "wactorz-node.service"
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
        f"{home}/wactorz/venv/bin/python {home}/wactorz/remote_runner.py "
        "--broker ${WACTORZ_BROKER} --port ${WACTORZ_PORT} --name ${WACTORZ_NODE}"
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


def user_unit_path(home: str) -> str:
    return f"{home}/.config/systemd/user/{UNIT_NAME}"


def unit_path(rung: Rung, home: str) -> str:
    return SYSTEM_UNIT_PATH if rung.system else user_unit_path(home)


def systemctl(rung: Rung) -> str:
    """The `systemctl` invocation that reaches this rung's manager."""
    if rung is USER:
        return f"{USER_ENV}systemctl --user"
    if rung is SUDO:
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
    user_manager, _ = await run(f"{USER_ENV}systemctl --user show --property=Version")
    if user_manager and await linger_enabled(run):
        return USER
    if (await run("sudo -n true"))[0]:
        return SUDO
    return NOHUP


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
    return (await run(f"{control} enable --now {UNIT_NAME}"))[0]


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
