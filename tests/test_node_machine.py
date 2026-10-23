"""What a node says its machine is, in the manifest it publishes retained.

Placement is judged from these fields, so each is either right or absent. The
readers look things up under a root they are given, so every case lays out the
files of a machine in a directory of its own -- a device tree, a cgroup, a few
device nodes -- rather than describing the machine the suite runs on.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from wactorz.node import machine, resources

MIB = resources.MIB


def _file(root: Path, path: str, text: str = "") -> Path:
    """``path``, absolute on a real machine, written under ``root``."""
    target = root / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read device paths as on Linux, whatever the suite runs on."""
    monkeypatch.setattr(machine, "system", lambda: "linux")


class TestArchitecture:
    def test_a_64_bit_interpreter_is_the_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(machine.platform, "machine", lambda: "aarch64")
        monkeypatch.setattr(machine.struct, "calcsize", lambda _fmt: 8)

        assert machine.arch() == "aarch64"

    def test_a_32_bit_system_on_a_64_bit_kernel_is_what_wheels_must_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Raspberry Pi's 64-bit kernel under a 32-bit OS says aarch64; every
        # wheel it can install is armv7l.
        monkeypatch.setattr(machine.platform, "machine", lambda: "aarch64")
        monkeypatch.setattr(machine.struct, "calcsize", lambda _fmt: 4)

        assert machine.arch() == "armv7l"

    @pytest.mark.parametrize(
        ("reported", "named"),
        [("arm64", "aarch64"), ("AMD64", "x86_64"), ("x86_64", "x86_64"), ("aarch64", "aarch64")],
    )
    def test_one_cpu_has_one_name_on_every_system(
        self, monkeypatch: pytest.MonkeyPatch, reported: str, named: str
    ) -> None:
        # macOS says arm64 and Windows AMD64 for what Linux calls aarch64 and
        # x86_64; a requirement names one of them.
        monkeypatch.setattr(machine.platform, "machine", lambda: reported)
        monkeypatch.setattr(machine.struct, "calcsize", lambda _fmt: 8)

        assert machine.arch() == named


class TestSystemRelease:
    def test_macos_by_its_own_version_not_the_kernels(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(machine, "system", lambda: "darwin")
        monkeypatch.setattr(machine.platform, "mac_ver", lambda: ("14.4", ("", "", ""), "arm64"))

        assert machine.os_release() == "macOS 14.4"

    def test_windows_by_release_and_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(machine, "system", lambda: "win32")
        monkeypatch.setattr(machine.platform, "release", lambda: "11")
        monkeypatch.setattr(machine.platform, "version", lambda: "10.0.26100")

        assert machine.os_release() == "Windows 11 (10.0.26100)"


class TestModel:
    def test_a_board_names_itself_in_the_device_tree(self, tmp_path: Path) -> None:
        _file(tmp_path, "/proc/device-tree/model", "Raspberry Pi 5 Model B Rev 1.0\x00")

        assert machine.model(tmp_path) == "Raspberry Pi 5 Model B Rev 1.0"

    def test_a_pc_names_itself_in_its_firmware(self, tmp_path: Path) -> None:
        _file(tmp_path, "/sys/class/dmi/id/product_name", "NUC13ANHi5\n")

        assert machine.model(tmp_path) == "NUC13ANHi5"

    def test_a_machine_that_says_nothing_has_no_model(self, tmp_path: Path) -> None:
        assert machine.model(tmp_path) is None


class TestContainer:
    @pytest.fixture(autouse=True)
    def _no_container_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("container", raising=False)
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)

    @pytest.mark.parametrize("marker", ["/.dockerenv", "/run/.containerenv"])
    def test_docker_and_podman_leave_a_marker(self, tmp_path: Path, marker: str) -> None:
        _file(tmp_path, marker)

        assert machine.in_container(tmp_path) is True

    def test_nspawn_and_podman_set_a_variable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("container", "podman")

        assert machine.in_container(tmp_path) is True

    def test_a_plain_machine_is_not_one(self, tmp_path: Path) -> None:
        assert machine.in_container(tmp_path) is False


class TestCpus:
    @staticmethod
    def _cgroup(root: Path, relative: str, cpu_max: str) -> None:
        _file(root, "/proc/self/cgroup", f"0::/{relative}\n")
        _file(root, f"/sys/fs/cgroup/{relative}/cpu.max".replace("//", "/"), cpu_max)

    @pytest.fixture(autouse=True)
    def _eight_cpus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            machine.os, "sched_getaffinity", lambda _pid: set(range(8)), raising=False
        )

    def test_a_quota_holds_it_to_fewer(self, tmp_path: Path) -> None:
        self._cgroup(tmp_path, "", "150000 100000")

        assert machine.cpu_count(tmp_path) == 2

    def test_no_quota_leaves_every_cpu(self, tmp_path: Path) -> None:
        self._cgroup(tmp_path, "", "max 100000")

        assert machine.cpu_count(tmp_path) == 8

    def test_a_quota_above_the_machine_changes_nothing(self, tmp_path: Path) -> None:
        self._cgroup(tmp_path, "", "1600000 100000")

        assert machine.cpu_count(tmp_path) == 8

    def test_a_quota_on_a_parent_binds_too(self, tmp_path: Path) -> None:
        self._cgroup(tmp_path, "pod", "100000 100000")
        self._cgroup(tmp_path, "pod/node", "max 100000")

        assert machine.cpu_count(tmp_path) == 1


class TestMemory:
    def test_a_limit_below_the_machine_is_the_total(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            machine.psutil, "virtual_memory", lambda: SimpleNamespace(total=8000 * MIB)
        )
        _file(tmp_path, "/proc/self/cgroup", "0::/\n")
        _file(tmp_path, "/sys/fs/cgroup/memory.max", str(512 * MIB))
        _file(tmp_path, "/sys/fs/cgroup/memory.current", str(100 * MIB))

        assert machine.ram_total_mb(tmp_path) == 512

    def test_without_a_limit_it_is_the_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            machine.psutil, "virtual_memory", lambda: SimpleNamespace(total=8000 * MIB)
        )

        assert machine.ram_total_mb(tmp_path) == 8000


class TestDisk:
    def test_a_directory_not_made_yet_is_measured_where_it_will_be(self, tmp_path: Path) -> None:
        state = tmp_path / "wactorz" / "state"

        described = machine.disk(state)

        assert described is not None
        assert described["path"] == str(state)
        assert described["total_mb"] >= described["free_mb"] > 0


@pytest.mark.usefixtures("linux")
class TestDevices:
    def test_each_kind_by_what_shows_it(self, tmp_path: Path) -> None:
        _file(tmp_path, "/sys/class/bluetooth/hci0")
        _file(tmp_path, "/run/dbus/system_bus_socket")
        _file(tmp_path, "/sys/class/video4linux/video0/device/uevent", "DRIVER=uvcvideo\n")
        _file(tmp_path, "/dev/snd/pcmC1D0c")
        _file(tmp_path, "/dev/snd/pcmC0D0p")
        _file(tmp_path, "/dev/ttyUSB0")
        _file(tmp_path, "/dev/gpiochip4")
        _file(tmp_path, "/dev/i2c-1")

        assert machine.devices(tmp_path) == list(machine.DEVICE_KINDS)

    def test_a_decoder_or_an_image_processor_is_not_a_camera(self, tmp_path: Path) -> None:
        # What a Raspberry Pi 5 with no camera attached has under video4linux.
        _file(tmp_path, "/sys/class/video4linux/video19/device/uevent", "DRIVER=rpi-hevc-dec\n")
        _file(tmp_path, "/sys/class/video4linux/video20/device/uevent", "DRIVER=pispbe\n")

        assert "camera" not in (machine.devices(tmp_path) or [])

    def test_playback_alone_is_not_a_microphone(self, tmp_path: Path) -> None:
        _file(tmp_path, "/dev/snd/pcmC0D0p")

        assert machine.devices(tmp_path) == ["speaker"]

    def test_an_adapter_without_bluez_cannot_be_used(self, tmp_path: Path) -> None:
        # A container sees the host's adapter in /sys, but not its system bus.
        _file(tmp_path, "/sys/class/bluetooth/hci0")

        assert "bluetooth" not in (machine.devices(tmp_path) or [])

    def test_none_present_is_an_empty_list(self, tmp_path: Path) -> None:
        assert machine.devices(tmp_path) == []

    def test_off_linux_it_cannot_tell(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(machine, "system", lambda: "win32")

        assert machine.devices(tmp_path) is None
        assert machine.accelerators(tmp_path) is None


@pytest.mark.usefixtures("linux")
class TestAccelerators:
    def test_an_nvidia_gpu_by_its_model(self, tmp_path: Path) -> None:
        _file(
            tmp_path,
            "/proc/driver/nvidia/gpus/0/information",
            "Model:           NVIDIA GeForce RTX 4060\nIRQ:             140\n",
        )

        assert machine.accelerators(tmp_path) == [
            {"kind": "cuda", "name": "NVIDIA GeForce RTX 4060"}
        ]

    def test_a_hailo_and_a_coral(self, tmp_path: Path) -> None:
        _file(tmp_path, "/dev/hailo0")
        _file(tmp_path, "/dev/apex_0")

        assert machine.accelerators(tmp_path) == [
            {"kind": "hailo", "name": "Hailo"},
            {"kind": "edgetpu", "name": "Coral Edge TPU"},
        ]

    def test_none_is_an_empty_list(self, tmp_path: Path) -> None:
        assert machine.accelerators(tmp_path) == []

    @pytest.mark.parametrize(
        ("cpu", "gpus"), [("arm64", [{"kind": "mps", "name": "Apple GPU"}]), ("x86_64", [])]
    )
    def test_a_mac_with_apple_silicon_has_its_gpu(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cpu: str, gpus: list[dict[str, str]]
    ) -> None:
        monkeypatch.setattr(machine, "system", lambda: "darwin")
        monkeypatch.setattr(machine.platform, "machine", lambda: cpu)
        monkeypatch.setattr(machine.struct, "calcsize", lambda _fmt: 8)

        assert machine.accelerators(tmp_path) == gpus


class TestPackages:
    def test_installed_packages_by_the_name_pip_compares(self) -> None:
        found = machine.packages()

        assert "psutil" in found
        assert all(name == machine.normalise(name) for name in found)

    @pytest.mark.parametrize(
        ("name", "normalised"),
        [
            ("PyYAML", "pyyaml"),
            ("opencv_python", "opencv-python"),
            ("zope.interface", "zope-interface"),
        ],
    )
    def test_names_are_normalised_as_pip_does(self, name: str, normalised: str) -> None:
        assert machine.normalise(name) == normalised


class TestTheManifest:
    def test_every_field_under_its_name(self, tmp_path: Path) -> None:
        described = machine.describe(tmp_path / "state", root=tmp_path)

        assert set(described) == {
            "manifest_v",
            "arch",
            "os",
            "os_release",
            "python",
            "model",
            "container",
            "cpu_count",
            "ram_total_mb",
            "swap_total_mb",
            "disk",
            "gpu",
            "devices",
            "packages",
        }
        assert described["manifest_v"] == machine.MANIFEST_VERSION
        assert set(described["disk"]) == {"state", "venv"}
