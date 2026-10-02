"""The list of third-party packages an image carries, and the license files beside it.

An image passes on every package installed in it. `scripts/third_party.py`
writes what each one is and under which license, from the metadata pip
installed; the Dockerfile runs it and keeps the result, with Wactorz's own
LICENSE and NOTICE, where someone looking in the image finds them.
"""

import importlib.util
from importlib.metadata import Distribution
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(name="third_party", scope="module")
def third_party_fixture() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "third_party", ROOT / "scripts" / "third_party.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _installed(tmp_path: Path, name: str, version: str, *metadata: str) -> Distribution:
    """A distribution as pip leaves one: a ``.dist-info`` folder with its METADATA."""
    info = tmp_path / f"{name}-{version}.dist-info"
    info.mkdir()
    body = "\n".join(["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}", *metadata])
    (info / "METADATA").write_bytes((body + "\n").encode("utf-8"))
    return Distribution.at(info)


class TestWhatAPackageSaysItsLicenseIs:
    def test_an_spdx_expression_is_taken_as_it_is(
        self, third_party: ModuleType, tmp_path: Path
    ) -> None:
        package = _installed(
            tmp_path,
            "demo",
            "1.0",
            "License-Expression: MIT OR Apache-2.0",
            "Classifier: License :: OSI Approved :: BSD License",
        )

        assert third_party.declared_license(package) == "MIT OR Apache-2.0"

    def test_without_one_the_classifiers_say(self, third_party: ModuleType, tmp_path: Path) -> None:
        package = _installed(
            tmp_path,
            "demo",
            "1.0",
            "Classifier: Programming Language :: Python :: 3",
            "Classifier: License :: OSI Approved :: BSD License",
            "Classifier: License :: OSI Approved :: MIT License",
        )

        assert third_party.declared_license(package) == "BSD License, MIT License"

    def test_a_license_pasted_whole_into_the_field_is_cut_to_its_first_line(
        self, third_party: ModuleType, tmp_path: Path
    ) -> None:
        package = _installed(
            tmp_path,
            "demo",
            "1.0",
            "License: Apache License, Version 2.0",
            "        Licensed under the Apache License, Version 2.0 (the License);",
            "        you may not use this file except in compliance with the License.",
        )

        assert third_party.declared_license(package) == "Apache License, Version 2.0"

    def test_a_package_that_declares_nothing_is_said_to(
        self, third_party: ModuleType, tmp_path: Path
    ) -> None:
        assert third_party.declared_license(_installed(tmp_path, "demo", "1.0")) == "not declared"


class TestTheList:
    def test_it_is_one_line_a_package_in_order_of_name(
        self, third_party: ModuleType, tmp_path: Path
    ) -> None:
        packages = [
            _installed(tmp_path, "zeta", "2.0", "License-Expression: MIT"),
            _installed(tmp_path, "Alpha", "1.5", "License-Expression: BSD-3-Clause"),
        ]

        assert third_party.lines(packages) == ["Alpha 1.5: BSD-3-Clause", "zeta 2.0: MIT"]

    def test_it_names_the_license_texts_a_package_installed(
        self, third_party: ModuleType, tmp_path: Path
    ) -> None:
        package = _installed(
            tmp_path,
            "demo",
            "1.0",
            "License-Expression: Apache-2.0",
            "License-File: NOTICE",
            "License-File: LICENSE",
        )

        assert third_party.lines([package]) == ["demo 1.0: Apache-2.0 (texts: LICENSE, NOTICE)"]

    def test_a_package_found_twice_is_listed_once(
        self, third_party: ModuleType, tmp_path: Path
    ) -> None:
        # Two folders on the path can hold the same distribution: what the
        # image shipped, and a copy an agent installed over it.
        first = tmp_path / "system"
        second = tmp_path / "user"
        first.mkdir()
        second.mkdir()
        packages = [
            _installed(first, "demo", "1.0", "License-Expression: MIT"),
            _installed(second, "demo", "1.1", "License-Expression: MIT"),
        ]

        assert third_party.lines(packages) == ["demo 1.0: MIT"]


class TestWhatTheImageCarries:
    def test_wactorz_is_built_with_its_license_and_notice_beside_it(self) -> None:
        # The package's metadata names both. A build that cannot find them
        # leaves them out without a word.
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        declared = project[project.index("license-files = [") :]
        declared = declared[: declared.index("]")]

        assert '"LICENSE"' in declared
        assert '"NOTICE.md"' in declared
        assert "COPY pyproject.toml README.md LICENSE NOTICE.md uv.lock ./" in DOCKERFILE

    def test_the_list_is_written_once_everything_is_installed(self) -> None:
        install = DOCKERFILE.index("pip install --no-cache-dir --no-deps .")
        written = DOCKERFILE.index("python scripts/third_party.py > /app/THIRD_PARTY.txt")

        assert install < written
        assert written < DOCKERFILE.index("rm -rf /tmp/requirements.txt"), (
            "the script is among the build inputs removed at the end of that step"
        )

    def test_the_build_inputs_are_removed_and_the_notices_are_not(self) -> None:
        removed = DOCKERFILE[DOCKERFILE.index("rm -rf /tmp/requirements.txt") :]
        removed = removed[: removed.index("\n\n")]

        assert "/app/scripts" in removed
        for kept in ("/app/LICENSE", "/app/NOTICE.md", "/app/THIRD_PARTY.txt"):
            assert kept not in removed
