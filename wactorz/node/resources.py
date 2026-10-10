"""What a node says in its heartbeat about how close it is to running out.

Whether an agent fits on a node is judged from these, so each one is either
right or absent: a reading the node cannot take is ``None``, never a guess and
never a zero that would read as "nothing left". Every reader takes the paths it
looks at as arguments, so a test can point it at a directory of its own.

Two readings are not what psutil says on its own:

- **Memory under a limit.** Inside a container, or under a systemd unit with
  ``MemoryMax=``, psutil still reports the whole machine, so a node held to
  512 MB would look as though it had gigabytes free. The tightest cgroup v2
  memory limit over this process wins when it is below the machine's memory.
- **Throttling** is what a Raspberry Pi's firmware reports through
  ``vcgencmd get_throttled``. Elsewhere there is no comparable signal: the
  "high" mark psutil gives with a temperature is whatever the sensor's driver
  filled in, which is often nonsense, so no flag is derived from it.

Everything here reads files or runs a short command, so :func:`read` is meant
to be called through ``asyncio.to_thread``.
"""

import contextlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import psutil

MIB = 1024 * 1024

#: Where cgroup v2 is mounted, and where the kernel says which cgroup this
#: process is in.
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_CGROUP = Path("/proc/self/cgroup")

#: Where hardware monitors appear. A Raspberry Pi's ``rpi_volt`` monitor raises
#: an alarm on under-voltage, readable without the firmware tool.
HWMON_ROOT = Path("/sys/class/hwmon")

#: Temperature sensors that measure the CPU, by the name psutil reports, in the
#: order they are preferred: a Raspberry Pi and most ARM boards, Intel, AMD.
CPU_SENSORS = ("cpu_thermal", "cpu-thermal", "soc_thermal", "coretemp", "k10temp")

#: The bits of ``vcgencmd get_throttled`` that describe the present, by name.
#: The higher bits say the same about any time since boot, which is history
#: rather than a reason to place an agent elsewhere.
THROTTLE_BITS = {0: "under_voltage", 1: "freq_capped", 2: "throttled", 3: "soft_temp_limit"}

#: How long ``vcgencmd`` may take. It answers in milliseconds; a firmware that
#: does not answer at all must not hold up the heartbeat behind it.
VCGENCMD_TIMEOUT_S = 5.0


def read(state_dir: Path) -> dict[str, Any]:
    """Every reading the heartbeat carries, under the names it carries them."""
    used, free = memory_mb()
    load = load_average()
    return {
        "cpu_pct": _guarded(lambda: psutil.cpu_percent(interval=None)),
        "mem_used_mb": used,
        "mem_free_mb": free,
        "swap_used_mb": _guarded(lambda: psutil.swap_memory().used // MIB),
        "load_1m": load[0] if load else None,
        "load_5m": load[1] if load else None,
        "disk_free_mb": disk_free_mb(state_dir),
        "temp_c": cpu_temp_c(),
        "throttled": throttled(),
    }


def memory_mb(
    cgroup_root: Path = CGROUP_ROOT, proc_cgroup: Path = PROC_CGROUP
) -> tuple[int | None, int | None]:
    """Memory in use and memory available, in MiB, within any limit on this process."""
    try:
        vm = psutil.virtual_memory()
    except Exception:
        return None, None
    used, available = vm.used, vm.available
    limited = cgroup_memory(cgroup_root, proc_cgroup)
    if limited is not None and limited[0] < vm.total:
        limit, used = limited
        available = min(available, max(limit - used, 0))
    return used // MIB, available // MIB


def cgroup_memory(cgroup_root: Path, proc_cgroup: Path) -> tuple[int, int] | None:
    """The tightest cgroup v2 memory limit over this process and what is used under it.

    Walks from the process's own cgroup up to the root, since a limit set on a
    parent (a systemd slice, a pod) binds as surely as one on the process's
    own. Usage leaves out the page cache the kernel can drop on demand, as
    ``docker stats`` does; otherwise a node that has read a large file looks
    full. None without cgroup v2, or with no limit anywhere on the way up.
    """
    tightest: tuple[int, Path] | None = None
    for candidate in cgroup_chain(cgroup_root, proc_cgroup):
        limit = _limit_at(candidate)
        if limit is not None and (tightest is None or limit < tightest[0]):
            tightest = (limit, candidate)
    if tightest is None:
        return None
    limit, where = tightest
    used = _int_file(where / "memory.current")
    if used is None:
        return None
    used -= _stat_value(where / "memory.stat", "inactive_file") or 0
    return limit, max(used, 0)


def cgroup_chain(cgroup_root: Path, proc_cgroup: Path) -> list[Path]:
    """This process's cgroup v2 directory and each above it, up to the root.

    Empty without cgroup v2. A limit set anywhere on the chain binds this
    process, so a reader of one looks at all of them.
    """
    try:
        line = next(
            (ln for ln in proc_cgroup.read_text().splitlines() if ln.startswith("0::")), None
        )
    except OSError:
        return []
    if line is None:
        return []
    relative = line[3:].strip().lstrip("/")
    directory = cgroup_root / relative if relative else cgroup_root
    chain = []
    for candidate in (directory, *directory.parents):
        chain.append(candidate)
        if candidate == cgroup_root:
            break
    return chain


def load_average() -> tuple[float, float] | None:
    """The one- and five-minute load averages, or None where there are none."""
    try:
        one, five, _ = psutil.getloadavg()
    except Exception:
        return None
    return round(one, 2), round(five, 2)


def disk_free_mb(path: Path) -> int | None:
    """Free space, in MiB, on the filesystem that holds ``path``.

    The state directory may not exist yet on a node that has never stored
    anything, so the nearest directory above it that does is measured instead:
    it is the same filesystem the state will be written to.
    """
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            try:
                return shutil.disk_usage(candidate).free // MIB
            except OSError:
                return None
    return None


def cpu_temp_c(sensors: dict[str, list[Any]] | None = None) -> float | None:
    """The CPU's temperature in °C, or None where no CPU sensor is known.

    Only sensors known to measure the CPU are read. The hottest of everything
    psutil lists would as often be a battery, a disk or a Wi-Fi card.
    """
    found = _sensors() if sensors is None else sensors
    if found is None:
        return None
    for name in CPU_SENSORS:
        readings = found.get(name)
        if readings:
            return round(float(readings[0].current), 1)
    return None


def _sensors() -> dict[str, list[Any]] | None:
    """Every temperature psutil can read here, or None where it reads none."""
    reader = getattr(psutil, "sensors_temperatures", None)  # absent on Windows and macOS
    if reader is None:
        return None
    try:
        return reader()
    except Exception:
        return None


def throttled(hwmon_root: Path = HWMON_ROOT) -> list[str] | None:
    """What is holding the board back right now, or None where it cannot tell.

    ``vcgencmd`` reports all four conditions. Without it (a container that has
    the Pi's ``/sys`` but not its firmware tool) the voltage monitor still says
    whether the supply is too weak, which is the one that crashes a board.
    """
    if shutil.which("vcgencmd"):
        flags = _vcgencmd_throttled()
        if flags is not None:
            return flags
    alarm = _rpi_volt_alarm(hwmon_root)
    if alarm is None:
        return None
    return ["under_voltage"] if alarm else []


def throttle_flags(value: int) -> list[str]:
    """The names of the present-tense bits set in a ``get_throttled`` value."""
    return [name for bit, name in THROTTLE_BITS.items() if value & (1 << bit)]


def _vcgencmd_throttled() -> list[str] | None:
    try:
        done = subprocess.run(
            ["vcgencmd", "get_throttled"],  # noqa: S607  # found on PATH just before, as on any Pi
            capture_output=True,
            text=True,
            timeout=VCGENCMD_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Answers "throttled=0x50005".
    _, _, value = done.stdout.strip().partition("=")
    try:
        return throttle_flags(int(value, 16))
    except ValueError:
        return None


def _rpi_volt_alarm(hwmon_root: Path) -> bool | None:
    try:
        monitors = list(hwmon_root.iterdir())
    except OSError:
        return None
    for monitor in monitors:
        with contextlib.suppress(OSError):
            if (monitor / "name").read_text().strip() == "rpi_volt":
                return _int_file(monitor / "in0_lcrit_alarm") == 1
    return None


def _limit_at(directory: Path) -> int | None:
    """The memory limit set at one cgroup, or None for none ("max") or no file."""
    try:
        text = (directory / "memory.max").read_text().strip()
    except OSError:
        return None
    return None if text == "max" else _to_int(text)


def _int_file(path: Path) -> int | None:
    try:
        return _to_int(path.read_text().strip())
    except OSError:
        return None


def _stat_value(path: Path, key: str) -> int | None:
    try:
        for line in path.read_text().splitlines():
            name, _, value = line.partition(" ")
            if name == key:
                return _to_int(value)
    except OSError:
        return None
    return None


def _to_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _guarded(reading: Any) -> Any:
    """``reading()``, or None if psutil cannot take it on this platform."""
    try:
        return reading()
    except Exception:
        return None
