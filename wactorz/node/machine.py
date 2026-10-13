"""What a node is: the machine it runs on, described once and published retained.

The heartbeat says how the machine is doing; this says what it is -- the
architecture a wheel has to match, the Python a package has to support, the
memory and disk there are in all, the devices an agent may need, the packages
already installed. These change rarely, so they travel on their own retained
topic (``nodes/<name>/manifest``) when the node starts and when an install
changes them, rather than on every heartbeat.

Whether an agent can run on a node is judged from these, so as in
:mod:`.resources` each field is either right or absent: ``None`` where the node
cannot tell, never a guess. Every reader takes the paths it looks at, so a
test can describe a machine of its own.

Everything here reads files, so :func:`describe` is meant to be called through
``asyncio.to_thread``.
"""

import contextlib
import math
import os
import platform
import re
import shutil
import struct
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

import psutil

from . import resources

#: The shape of the manifest. Raised when a field changes meaning, so a reader
#: can tell an older node's manifest from one it does not understand.
MANIFEST_VERSION = 1

#: Where a single-board computer names itself, and where a PC does.
DEVICE_TREE_MODEL = Path("/proc/device-tree/model")
DMI_PRODUCT = Path("/sys/class/dmi/id/product_name")

#: Signs of running in a container: Docker's marker file, Podman's.
CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))

#: What an agent can use, by kind, in the order the manifest lists them. Each
#: is present when something an agent could open is there, not when the
#: kernel merely has a node for it.
DEVICE_KINDS = ("bluetooth", "camera", "microphone", "speaker", "serial", "gpio", "i2c")

#: Device kinds shown by a device node alone. Sound nodes end in ``c`` for
#: capture and ``p`` for playback.
DEVICE_PATTERNS: dict[str, tuple[str, ...]] = {
    "microphone": ("/dev/snd/pcmC*D*c",),
    "speaker": ("/dev/snd/pcmC*D*p",),
    "serial": ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"),
    "gpio": ("/dev/gpiochip*",),
    "i2c": ("/dev/i2c-*",),
}

#: A Bluetooth adapter, and the system bus BlueZ answers on. An agent reaches
#: Bluetooth through BlueZ, so an adapter without the bus -- a container shows
#: the host's adapter in ``/sys`` -- is one it cannot use.
BLUETOOTH_ADAPTERS = "/sys/class/bluetooth/hci*"
SYSTEM_BUS = Path("/run/dbus/system_bus_socket")

#: Where the kernel lists video devices, each naming its driver in ``uevent``.
VIDEO_DEVICES = "/sys/class/video4linux/video*"

#: Drivers of video devices that are cameras an agent can open: USB webcams
#: and the camera receivers of the boards nodes run on. Not every video device
#: is one -- a Raspberry Pi 5 has a dozen for its decoder and image processor,
#: and a laptop's raw sensor nodes need libcamera before anything can read
#: them -- so a driver not named here is not taken for a camera.
CAMERA_DRIVERS = frozenset(
    {
        "uvcvideo",  # USB webcams
        "rp1-cfe",  # Raspberry Pi 5 camera connector
        "unicam",  # Raspberry Pi 4 and earlier
        "bcm2835-unicam",
        "bcm2835-v4l2",  # the legacy Raspberry Pi camera stack
        "tegra-video",  # NVIDIA Jetson camera connector
    }
)

#: Accelerators by the paths that show them, as (kind, pattern).
ACCELERATOR_PATTERNS = (
    ("cuda", "/proc/driver/nvidia/gpus/*"),
    ("cuda", "/dev/nvhost-ctrl"),  # an NVIDIA Jetson
    ("hailo", "/dev/hailo*"),
    ("edgetpu", "/dev/apex_*"),  # a Coral on PCIe
)

#: One name for each CPU, whatever the system calls it: macOS says ``arm64``
#: and Windows ``AMD64`` for what Linux calls ``aarch64`` and ``x86_64``.
ARCH_NAMES = {"arm64": "aarch64", "amd64": "x86_64", "x64": "x86_64"}

#: What a 64-bit machine runs a 32-bit interpreter as -- the one a wheel has
#: to match.
THIRTY_TWO_BIT_USERLAND = {"aarch64": "armv7l", "x86_64": "i686"}


def describe(state_dir: Path, root: Path = Path("/")) -> dict[str, Any]:
    """The manifest for this machine, without the node's identity fields.

    ``root`` is where the filesystem paths above are looked up, so a test can
    lay out a machine in a directory of its own.
    """
    return {
        "manifest_v": MANIFEST_VERSION,
        "arch": arch(),
        "os": platform.system().lower() or None,
        "os_release": os_release(),
        "python": platform.python_version(),
        "model": model(root),
        "container": in_container(root),
        "cpu_count": cpu_count(root),
        "ram_total_mb": ram_total_mb(root),
        "swap_total_mb": _guarded(lambda: psutil.swap_memory().total // resources.MIB),
        "disk": {"state": disk(state_dir), "venv": disk(Path(sys.prefix))},
        "gpu": accelerators(root),
        "devices": devices(root),
        "packages": packages(),
    }


def arch() -> str:
    """The architecture this interpreter runs as.

    Not the kernel's: a Raspberry Pi with a 64-bit kernel and a 32-bit system
    says ``aarch64`` while every wheel it can install is ``armv7l``. Named
    the same way on every system, so one name in a requirement covers them.
    """
    machine = platform.machine().lower()
    machine = ARCH_NAMES.get(machine, machine)
    if struct.calcsize("P") * 8 == 32:
        return THIRTY_TWO_BIT_USERLAND.get(machine, machine)
    return machine


def os_release() -> str | None:
    """The system and its version, such as "Debian GNU/Linux 12 (bookworm)" or "macOS 14.4"."""
    if on_linux():
        with contextlib.suppress(OSError):
            return platform.freedesktop_os_release().get("PRETTY_NAME")
        return None
    if system() == "darwin":
        # `platform.release()` here is the Darwin kernel's version, not macOS's.
        version = platform.mac_ver()[0]
        return f"macOS {version}" if version else None
    if system() == "win32":
        return f"Windows {platform.release()} ({platform.version()})"
    return None


def model(root: Path) -> str | None:
    """What the hardware calls itself, for a person to read; None where it says nothing."""
    for path in (DEVICE_TREE_MODEL, DMI_PRODUCT):
        with contextlib.suppress(OSError):
            # The device tree ends the string with a NUL.
            text = _under(root, path).read_text(errors="replace").strip("\x00\n ")
            if text:
                return text
    return None


def in_container(root: Path) -> bool:
    """Whether this node runs in a container, where the machine's limits are not its own."""
    # systemd-nspawn and Podman set `container`, lower case; Kubernetes sets its own.
    if os.environ.get("container") or os.environ.get("KUBERNETES_SERVICE_HOST"):  # noqa: SIM112
        return True
    return any(_under(root, marker).exists() for marker in CONTAINER_MARKERS)


def cpu_count(root: Path) -> int | None:
    """The CPUs this process may use: within its affinity and any cgroup CPU limit."""
    affinity = getattr(os, "sched_getaffinity", None)  # Linux only
    count = len(affinity(0)) if affinity is not None else os.cpu_count()
    limit = cgroup_cpu_limit(*_cgroup_paths(root))
    if limit is not None and (count is None or limit < count):
        return limit
    return count


def cgroup_cpu_limit(cgroup_root: Path, proc_cgroup: Path) -> int | None:
    """Whole CPUs the tightest cgroup v2 ``cpu.max`` quota allows this process, rounded up."""
    limits = [
        limit
        for directory in resources.cgroup_chain(cgroup_root, proc_cgroup)
        if (limit := _cpu_limit_at(directory)) is not None
    ]
    return min(limits) if limits else None


def _cpu_limit_at(directory: Path) -> int | None:
    """The CPUs one cgroup's ``cpu.max`` allows, or None for none ("max") or no file."""
    try:
        quota, _, period = (directory / "cpu.max").read_text().strip().partition(" ")
    except OSError:
        return None
    if quota == "max":
        return None
    try:
        return max(1, math.ceil(int(quota) / int(period or "100000")))
    except (ValueError, ZeroDivisionError):
        return None


def ram_total_mb(root: Path) -> int | None:
    """The memory there is in all, in MiB: the cgroup limit when it is the smaller."""
    try:
        total = psutil.virtual_memory().total
    except Exception:
        return None
    limited = resources.cgroup_memory(*_cgroup_paths(root))
    if limited is not None:
        total = min(total, limited[0])
    return total // resources.MIB


def disk(path: Path) -> dict[str, Any] | None:
    """The filesystem that holds ``path``: its size and what is free on it, in MiB."""
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            try:
                usage = shutil.disk_usage(candidate)
            except OSError:
                return None
            return {
                "path": str(path),
                "total_mb": usage.total // resources.MIB,
                "free_mb": usage.free // resources.MIB,
            }
    return None


def accelerators(root: Path) -> list[dict[str, Any]] | None:
    """The compute accelerators present, ``[]`` for none; None where it cannot tell.

    On a Mac with Apple silicon that is its GPU, which PyTorch reaches as
    ``mps``; elsewhere off Linux the node has nothing to look at.
    """
    if system() == "darwin":
        return [{"kind": "mps", "name": "Apple GPU"}] if arch() == "aarch64" else []
    if not on_linux():
        return None
    found = []
    for kind, pattern in ACCELERATOR_PATTERNS:
        for path in sorted(_glob(root, pattern)):
            found.append({"kind": kind, "name": _accelerator_name(kind, path)})
    return found


def devices(root: Path) -> list[str] | None:
    """The device kinds present, by name; None off Linux, where it cannot tell."""
    if not on_linux():
        return None
    present = {
        kind for kind, patterns in DEVICE_PATTERNS.items() if any(_glob(root, p) for p in patterns)
    }
    if _glob(root, BLUETOOTH_ADAPTERS) and _under(root, SYSTEM_BUS).exists():
        present.add("bluetooth")
    if any(_video_driver(device) in CAMERA_DRIVERS for device in _glob(root, VIDEO_DEVICES)):
        present.add("camera")
    return [kind for kind in DEVICE_KINDS if kind in present]


def _video_driver(device: Path) -> str | None:
    """The driver behind one ``/sys/class/video4linux`` entry, from its ``uevent``."""
    with contextlib.suppress(OSError):
        for line in (device / "device" / "uevent").read_text().splitlines():
            key, _, value = line.partition("=")
            if key == "DRIVER":
                return value.strip()
    return None


def packages() -> dict[str, str]:
    """Every installed distribution in this node's environment, by normalised name."""
    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.name
        if name:
            found[normalise(name)] = dist.version
    return dict(sorted(found.items()))


def normalise(name: str) -> str:
    """A distribution name as pip compares them: lower case, runs of ``-_.`` as one ``-``."""
    return re.sub(r"[-_.]+", "-", name).lower()


def system() -> str:
    """``sys.platform``, asked through here so a test can be any system."""
    return sys.platform


def on_linux() -> bool:
    """Whether the device and accelerator paths here mean anything on this machine."""
    return system().startswith("linux")


def _cgroup_paths(root: Path) -> tuple[Path, Path]:
    """Where cgroup v2 and this process's cgroup are looked up under ``root``."""
    return _under(root, resources.CGROUP_ROOT), _under(root, resources.PROC_CGROUP)


def _accelerator_name(kind: str, path: Path) -> str:
    """What an accelerator calls itself, where it says; otherwise its kind."""
    if kind == "cuda" and path.is_dir():
        with contextlib.suppress(OSError):
            for line in (path / "information").read_text().splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "Model":
                    return value.strip()
    return {"cuda": "NVIDIA GPU", "hailo": "Hailo", "edgetpu": "Coral Edge TPU"}.get(kind, kind)


def _glob(root: Path, pattern: str) -> list[Path]:
    """``pattern``, an absolute path with wildcards, matched under ``root``."""
    try:
        return list(root.glob(pattern.lstrip("/")))
    except OSError:
        return []


def _under(root: Path, path: Path) -> Path:
    """``path``, absolute on a real machine, looked up under ``root``."""
    return root.joinpath(*path.parts[1:]) if root != Path("/") else path


def _guarded(reading: Any) -> Any:
    try:
        return reading()
    except Exception:
        return None
