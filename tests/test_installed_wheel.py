"""A wheel can be put together again from a package as it is installed.

`/deploy` gives a node the package the server runs. A server installed from a
branch or a commit has no checkout to build a wheel from, and the package index
has other code under its version number, so the node is handed a wheel made
from the server's own installed files.

The install is a made-up one in a temporary folder, laid out as pip lays one
out: the package, a data folder beside it, and the metadata folder with its
record of every file.
"""

import base64
import hashlib
import importlib.metadata
import json
import zipfile
from pathlib import Path

import pytest

from wactorz.core import installed_wheel
from wactorz.core.installed_wheel import installed_from_a_direct_reference, wheel_of_installed

FROM_A_COMMIT = {
    "url": "https://example.test/repo.git",
    "vcs_info": {"vcs": "git", "commit_id": "abc"},
}
EDITABLE = {"url": "file:///src/demo", "dir_info": {"editable": True}}
FROM_A_FOLDER = {"url": "file:///src/demo", "dir_info": {}}

FILES = {
    "demo/__init__.py": "VERSION = 1\n",
    "demo/node/__init__.py": "",
    "demo/__pycache__/__init__.cpython-313.pyc": "compiled",
    "static/app.js": "console.log(1)\n",
    "demo-1.2.3.dist-info/METADATA": "Metadata-Version: 2.4\nName: demo\nVersion: 1.2.3\n",
    "demo-1.2.3.dist-info/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    "demo-1.2.3.dist-info/entry_points.txt": "[console_scripts]\ndemo-node = demo.node:main\n",
    "demo-1.2.3.dist-info/INSTALLER": "pip\n",
    "demo-1.2.3.dist-info/REQUESTED": "",
}


def _install(site: Path, origin: dict | None, wheel: str | None = None) -> None:
    """Lay the made-up package out under ``site`` and have it be what `demo` resolves to."""
    files = dict(FILES)
    if wheel is not None:
        files["demo-1.2.3.dist-info/WHEEL"] = wheel
    if origin is not None:
        files["demo-1.2.3.dist-info/direct_url.json"] = json.dumps(origin)
    for name, text in files.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # As an installer records them: the files, the command it generated beside
    # the environment, and the record itself.
    recorded = [*files, "../../bin/demo-node", "demo-1.2.3.dist-info/RECORD"]
    (site / "demo-1.2.3.dist-info" / "RECORD").write_text(
        "".join(f"{name},,\n" for name in recorded), encoding="utf-8"
    )


@pytest.fixture(name="site")
def site_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    site = tmp_path / "site-packages"
    real = importlib.metadata.distribution

    def _distribution(name: str) -> importlib.metadata.Distribution:
        if name != "demo":
            return real(name)
        info = site / "demo-1.2.3.dist-info"
        if not info.is_dir():
            raise importlib.metadata.PackageNotFoundError(name)
        return importlib.metadata.Distribution.at(info)

    monkeypatch.setattr(installed_wheel.importlib.metadata, "distribution", _distribution)
    return site


class TestWhereItCameFrom:
    @pytest.mark.parametrize("origin", [FROM_A_COMMIT, FROM_A_FOLDER])
    def test_a_commit_or_a_folder_is_a_direct_reference(self, site: Path, origin: dict) -> None:
        _install(site, origin)

        assert installed_from_a_direct_reference("demo") is True

    def test_a_package_index_is_not(self, site: Path) -> None:
        # The index has this very code under this very version.
        _install(site, None)

        assert installed_from_a_direct_reference("demo") is False

    def test_an_editable_install_is_not(self, site: Path) -> None:
        # It points at a source tree and has no files of its own to pack.
        _install(site, EDITABLE)

        assert installed_from_a_direct_reference("demo") is False

    def test_a_package_that_is_not_installed_is_not(self, site: Path) -> None:
        assert installed_from_a_direct_reference("demo") is False


class TestTheWheel:
    def test_it_carries_the_package_and_is_named_as_a_wheel_is(
        self, site: Path, tmp_path: Path
    ) -> None:
        _install(site, FROM_A_COMMIT)

        wheel = wheel_of_installed("demo", tmp_path / "out")

        assert wheel is not None
        assert wheel.name == "demo-1.2.3-py3-none-any.whl"
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            assert archive.read("demo/__init__.py") == b"VERSION = 1\n"
        assert {"demo/__init__.py", "demo/node/__init__.py", "static/app.js"} <= names
        assert "demo-1.2.3.dist-info/entry_points.txt" in names

    def test_what_belongs_to_the_install_is_left_out(self, site: Path, tmp_path: Path) -> None:
        _install(site, FROM_A_COMMIT)

        wheel = wheel_of_installed("demo", tmp_path / "out")

        assert wheel is not None
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        assert not [name for name in names if name.endswith(".pyc") or ".." in name]
        for left_out in ("INSTALLER", "REQUESTED", "direct_url.json"):
            assert f"demo-1.2.3.dist-info/{left_out}" not in names

    def test_its_record_lists_every_file_with_its_hash(self, site: Path, tmp_path: Path) -> None:
        _install(site, FROM_A_COMMIT)

        wheel = wheel_of_installed("demo", tmp_path / "out")

        assert wheel is not None
        with zipfile.ZipFile(wheel) as archive:
            record = archive.read("demo-1.2.3.dist-info/RECORD").decode("utf-8").splitlines()
            listed = {line.split(",")[0]: line.split(",")[1:] for line in record}
            assert set(listed) == set(archive.namelist())
            data = archive.read("demo/__init__.py")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        assert listed["demo/__init__.py"] == [f"sha256={digest}", str(len(data))]
        assert listed["demo-1.2.3.dist-info/RECORD"] == ["", ""]

    def test_a_package_built_for_one_platform_is_not_repacked(
        self, site: Path, tmp_path: Path
    ) -> None:
        # Its compiled files are this machine's, and the node may be another kind.
        _install(site, FROM_A_COMMIT, wheel="Wheel-Version: 1.0\nTag: cp313-cp313-linux_x86_64\n")

        assert wheel_of_installed("demo", tmp_path / "out") is None

    def test_a_package_that_is_not_installed_has_no_wheel(self, site: Path, tmp_path: Path) -> None:
        assert wheel_of_installed("demo", tmp_path / "out") is None
