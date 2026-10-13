"""What a node reports about how close it is to running out.

Main places agents from these readings, so each is either right or absent. The
readers take the paths they look at, so every case here builds the files a
machine would have in a directory of its own -- a cgroup tree, a hardware
monitor -- rather than reading the machine the suite happens to run on.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from wactorz.node import resources

MIB = resources.MIB


def _cgroup(root: Path, relative: str, **files: str) -> Path:
    """One cgroup directory under ``root``, holding ``files``."""
    directory = root / relative if relative else root
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (directory / name.replace("_", ".", 1)).write_text(text)
    return directory


def _proc(tmp_path: Path, relative: str) -> Path:
    """A ``/proc/self/cgroup`` naming ``relative`` as this process's cgroup v2."""
    path = tmp_path / "proc_cgroup"
    path.write_text(f"0::/{relative}\n")
    return path


def _machine(monkeypatch: pytest.MonkeyPatch, total_mb: int, used_mb: int, free_mb: int) -> None:
    memory = SimpleNamespace(total=total_mb * MIB, used=used_mb * MIB, available=free_mb * MIB)
    monkeypatch.setattr(resources.psutil, "virtual_memory", lambda: memory)


class TestMemory:
    def test_without_a_limit_it_is_the_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _machine(monkeypatch, total_mb=8000, used_mb=3000, free_mb=5000)
        root = tmp_path / "cgroup"
        _cgroup(root, "system.slice/wactorz.service", memory_max="max", memory_current="1")

        used, free = resources.memory_mb(root, _proc(tmp_path, "system.slice/wactorz.service"))

        assert (used, free) == (3000, 5000)

    def test_a_container_limit_is_what_is_left(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The case the reading exists for: psutil says gigabytes, the container
        # is held to 512 MB and has used 400 of it.
        _machine(monkeypatch, total_mb=8000, used_mb=3000, free_mb=5000)
        root = tmp_path / "cgroup"
        _cgroup(root, "", memory_max=str(512 * MIB), memory_current=str(400 * MIB))

        used, free = resources.memory_mb(root, _proc(tmp_path, ""))

        assert (used, free) == (400, 112)

    def test_a_limit_on_a_parent_binds_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _machine(monkeypatch, total_mb=8000, used_mb=3000, free_mb=5000)
        root = tmp_path / "cgroup"
        _cgroup(root, "pod", memory_max=str(1024 * MIB), memory_current=str(256 * MIB))
        _cgroup(root, "pod/node", memory_max="max", memory_current=str(200 * MIB))

        used, free = resources.memory_mb(root, _proc(tmp_path, "pod/node"))

        assert (used, free) == (256, 768)

    def test_dropable_page_cache_is_not_counted_as_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _machine(monkeypatch, total_mb=8000, used_mb=3000, free_mb=5000)
        root = tmp_path / "cgroup"
        _cgroup(
            root,
            "",
            memory_max=str(512 * MIB),
            memory_current=str(500 * MIB),
            memory_stat=f"anon {300 * MIB}\ninactive_file {200 * MIB}\n",
        )

        used, free = resources.memory_mb(root, _proc(tmp_path, ""))

        assert (used, free) == (300, 212)

    def test_a_limit_above_the_machine_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _machine(monkeypatch, total_mb=4000, used_mb=1000, free_mb=3000)
        root = tmp_path / "cgroup"
        _cgroup(root, "", memory_max=str(64_000 * MIB), memory_current=str(10 * MIB))

        assert resources.memory_mb(root, _proc(tmp_path, "")) == (1000, 3000)

    def test_without_cgroup_v2_it_is_the_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _machine(monkeypatch, total_mb=4000, used_mb=1000, free_mb=3000)

        assert resources.memory_mb(tmp_path / "none", tmp_path / "no_proc") == (1000, 3000)

    def test_unreadable_memory_is_not_known_rather_than_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fails() -> None:
            raise OSError("no /proc")

        monkeypatch.setattr(resources.psutil, "virtual_memory", _fails)

        assert resources.memory_mb() == (None, None)


class TestDisk:
    def test_free_space_where_the_state_is(self, tmp_path: Path) -> None:
        free = resources.disk_free_mb(tmp_path)

        assert isinstance(free, int)
        assert free > 0

    def test_a_state_directory_not_made_yet_measures_the_one_above(self, tmp_path: Path) -> None:
        assert resources.disk_free_mb(tmp_path / "wactorz" / "state") == resources.disk_free_mb(
            tmp_path
        )


class TestTemperature:
    @staticmethod
    def _sensor(current: float) -> list[Any]:
        return [SimpleNamespace(label="", current=current, high=None, critical=None)]

    def test_a_pi_reads_its_cpu(self) -> None:
        sensors = {"rp1_adc": self._sensor(57.2), "cpu_thermal": self._sensor(60.05)}

        assert resources.cpu_temp_c(sensors) == 60.0

    def test_not_the_hottest_thing_listed(self) -> None:
        # A disk or a battery is not what throttles the CPU.
        sensors = {"nvme": self._sensor(80.0), "coretemp": self._sensor(55.0)}

        assert resources.cpu_temp_c(sensors) == 55.0

    def test_no_cpu_sensor_is_not_known(self) -> None:
        assert resources.cpu_temp_c({"BAT0": self._sensor(28.3)}) is None


class TestThrottling:
    @pytest.mark.parametrize(
        ("value", "flags"),
        [
            (0x0, []),
            (0x1, ["under_voltage"]),
            (0x6, ["freq_capped", "throttled"]),
            (0xF, ["under_voltage", "freq_capped", "throttled", "soft_temp_limit"]),
            # Only since boot: history, not the present.
            (0x50000, []),
        ],
    )
    def test_the_present_bits_by_name(self, value: int, flags: list[str]) -> None:
        assert resources.throttle_flags(value) == flags

    def test_the_firmware_answer_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(resources.shutil, "which", lambda _name: "/usr/bin/vcgencmd")
        monkeypatch.setattr(
            resources.subprocess,
            "run",
            lambda *_a, **_k: SimpleNamespace(stdout="throttled=0x50005\n"),
        )

        assert resources.throttled() == ["under_voltage", "throttled"]

    def test_a_firmware_that_hangs_falls_back_to_the_voltage_monitor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _hangs(*_a: Any, **_k: Any) -> None:
            raise subprocess.TimeoutExpired("vcgencmd", resources.VCGENCMD_TIMEOUT_S)

        monkeypatch.setattr(resources.shutil, "which", lambda _name: "/usr/bin/vcgencmd")
        monkeypatch.setattr(resources.subprocess, "run", _hangs)
        monitor = tmp_path / "hwmon3"
        monitor.mkdir()
        (monitor / "name").write_text("rpi_volt\n")
        (monitor / "in0_lcrit_alarm").write_text("1\n")

        assert resources.throttled(tmp_path) == ["under_voltage"]

    def test_the_voltage_monitor_alone_says_when_all_is_well(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(resources.shutil, "which", lambda _name: None)
        monitor = tmp_path / "hwmon0"
        monitor.mkdir()
        (monitor / "name").write_text("rpi_volt\n")
        (monitor / "in0_lcrit_alarm").write_text("0\n")

        assert resources.throttled(tmp_path) == []

    def test_a_machine_that_cannot_tell_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # None, not []: "nothing is wrong" is a claim this machine cannot make.
        monkeypatch.setattr(resources.shutil, "which", lambda _name: None)
        monitor = tmp_path / "hwmon0"
        monitor.mkdir()
        (monitor / "name").write_text("coretemp\n")

        assert resources.throttled(tmp_path) is None


class TestTheHeartbeatReadings:
    def test_every_reading_under_its_name(self, tmp_path: Path) -> None:
        readings = resources.read(tmp_path)

        assert set(readings) == {
            "cpu_pct",
            "mem_used_mb",
            "mem_free_mb",
            "swap_used_mb",
            "load_1m",
            "load_5m",
            "disk_free_mb",
            "temp_c",
            "throttled",
        }
        assert isinstance(readings["disk_free_mb"], int)

    def test_a_reading_psutil_cannot_take_is_not_known(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fails() -> None:
            raise RuntimeError("not on this platform")

        monkeypatch.setattr(resources.psutil, "getloadavg", _fails)
        monkeypatch.setattr(resources.psutil, "swap_memory", _fails)

        readings = resources.read(tmp_path)

        assert (readings["load_1m"], readings["load_5m"], readings["swap_used_mb"]) == (
            None,
            None,
            None,
        )
