"""The add-on version is allowed to move on its own, and the script must let it.

An add-on rebuild that ships no new library code — a `run.sh` fix, a schema
entry — is published by bumping only the add-on: `0.5.3` becomes `0.5.3.1`, with
no tag and no release. `sync_versions.py` used to rewrite that manifest
unconditionally, so the next sync silently dragged the add-on back to three
parts and un-published the rebuild.

Also pins the argument check. The script takes a bare positional version and
hands it to five writers on trust, so a mistyped flag was written into
`_version.py`, `package.json`, both add-on manifests and the docs as if it were
a version number.
"""

import importlib.util
import os
import subprocess
import sys
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    """Import `scripts/sync_versions.py`, which is not an installed module."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "scripts", "sync_versions.py")
    spec = importlib.util.spec_from_file_location("sync_versions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync_versions = _load_script()


class TestAnAddOnOnlyRebuild:
    """The fourth component counts rebuilds of one library version."""

    @pytest.mark.parametrize("current", ["0.5.3.1", "0.5.3.9", "0.5.3.12"])
    def test_it_survives_a_sync_to_the_same_library_version(self, current: str) -> None:
        assert sync_versions.addon_version_for(current, "0.5.3") == current

    @pytest.mark.parametrize("target", ["0.5.4", "0.6.0", "1.0.0"])
    def test_it_does_not_survive_a_new_library_version(self, target: str) -> None:
        # The suffix means "rebuild number of 0.5.3" and means nothing once the
        # library moves, so carrying it forward would claim a rebuild that never
        # happened.
        assert sync_versions.addon_version_for("0.5.3.1", target) == target

    def test_a_suffix_from_another_version_is_not_kept(self) -> None:
        # 0.5.2.1 against 0.5.3 is not a rebuild of 0.5.3 — the first three
        # parts have to match for the suffix to mean anything.
        assert sync_versions.addon_version_for("0.5.2.1", "0.5.3") == "0.5.3"


class TestAnOrdinaryVersion:
    def test_three_parts_are_replaced(self) -> None:
        assert sync_versions.addon_version_for("0.5.3", "0.5.4") == "0.5.4"

    def test_syncing_to_what_is_already_there_changes_nothing(self) -> None:
        assert sync_versions.addon_version_for("0.5.3", "0.5.3") == "0.5.3"

    def test_a_manifest_with_no_version_takes_the_new_one(self) -> None:
        assert sync_versions.addon_version_for("", "0.5.4") == "0.5.4"


class TestTheArgumentIsChecked:
    """There are no flags to parse, so anything passed is taken as the version."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run(
            [sys.executable, os.path.join("scripts", "sync_versions.py"), *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

    @pytest.mark.parametrize("argument", ["--check", "--dry-run", "-v", "latest", ""])
    def test_a_non_version_is_refused(self, argument: str) -> None:
        # Refused *before* any file is written: the failure this prevents is a
        # flag landing in five files at once, which reads as a version bump.
        result = self._run(argument)

        assert result.returncode == 1
        assert "Usage" in result.stdout

    def test_no_argument_is_refused(self) -> None:
        assert self._run().returncode == 1
