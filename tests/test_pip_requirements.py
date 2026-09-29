"""Whether an agent's declared packages are installed, and what an install broke.

Agents declare pip requirements, not module names: `reachy-mini==1.8.4`,
`deepgram-sdk>=3,<4`, `pillow`. The check reads installed metadata, so a pin or
a range is honoured and a pip name need not match the module it provides.

An install can also replace a package this process has already imported. That
is reported, because nothing but a restart makes the new version usable.
"""

import importlib.metadata
import sys
import types

import pytest

from wactorz.core import pip
from wactorz.core.pip import (
    install_wait_s,
    missing_requirements,
    requirement_is_satisfied,
    requirement_name,
    stale_loaded_distributions,
)


class TestRequirementName:
    @pytest.mark.parametrize(
        ("requirement", "name"),
        [
            ("reachy-mini==1.8.4", "reachy-mini"),
            ("deepgram-sdk>=3,<4", "deepgram-sdk"),
            ("Pillow[extra]>=1", "pillow"),
            ("typing_extensions", "typing-extensions"),
            ("pytest; python_version >= '3.8'", "pytest"),
        ],
    )
    def test_extras_versions_and_markers_are_dropped(self, requirement: str, name: str) -> None:
        assert requirement_name(requirement) == name


class TestRequirementIsSatisfied:
    def test_an_installed_distribution_is_satisfied(self) -> None:
        assert requirement_is_satisfied("pytest")

    def test_a_missing_distribution_is_not(self) -> None:
        assert not requirement_is_satisfied("definitely-not-installed-anywhere>=1.0")

    def test_an_exact_pin_must_match_the_installed_version(self) -> None:
        installed = importlib.metadata.version("pytest")

        assert requirement_is_satisfied(f"pytest=={installed}")
        assert not requirement_is_satisfied("pytest==0.0.1")

    def test_a_range_is_checked_against_the_installed_version(self) -> None:
        assert requirement_is_satisfied("pytest>=1,<1000")
        assert not requirement_is_satisfied("pytest>=1000")

    def test_extras_and_markers_do_not_stop_the_check(self) -> None:
        assert requirement_is_satisfied("pytest[testing]>=1; python_version >= '3.8'")

    def test_a_pip_name_unlike_its_module_is_found_by_metadata(self) -> None:
        # The distribution is `pluggy`-style metadata, whatever it imports as;
        # typing-extensions installs the module `typing_extensions`.
        assert requirement_is_satisfied("typing-extensions")

    def test_a_module_without_metadata_falls_back_to_importing(self) -> None:
        # The standard library has no distribution metadata.
        assert requirement_is_satisfied("json")

    def test_a_pin_on_a_module_without_metadata_is_not_satisfied(self) -> None:
        assert not requirement_is_satisfied("json==1.0")

    def test_the_fallback_maps_a_pip_name_to_its_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        imported: list[str] = []

        def _no_metadata(_name: str) -> str:
            raise importlib.metadata.PackageNotFoundError

        def _import(name: str) -> object:
            imported.append(name)
            return object()

        monkeypatch.setattr(importlib.metadata, "version", _no_metadata)
        monkeypatch.setattr("importlib.import_module", _import)

        assert requirement_is_satisfied("webrtcvad-wheels")
        assert requirement_is_satisfied("some-package")
        assert imported == ["webrtcvad", "some_package"]


def test_missing_requirements_keeps_order_and_skips_blanks() -> None:
    assert missing_requirements(["pytest", "", "nope-not-here", "json", "nope-two==1"]) == [
        "nope-not-here",
        "nope-two==1",
    ]


def test_the_wait_covers_every_package_the_installer_may_take() -> None:
    assert install_wait_s(6) > 6 * pip.PIP_INSTALL_TIMEOUT_S
    assert install_wait_s(0) > pip.PIP_INSTALL_TIMEOUT_S


class TestStaleLoadedDistributions:
    """The Reachy Mini SDK pins websockets<16; a fresh Wactorz install has 17."""

    @pytest.fixture(autouse=True)
    def _fake_package(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "fakews", types.ModuleType("fakews"))
        monkeypatch.setattr(
            importlib.metadata,
            "packages_distributions",
            lambda: {"fakews": ["FakeWS"], "notloaded": ["not-loaded"]},
        )

    def test_a_loaded_package_pip_replaced_is_reported(self) -> None:
        before = {"fakews": "17.1", "not-loaded": "1.0"}
        after = {"fakews": "15.0.1", "not-loaded": "1.0"}

        assert stale_loaded_distributions(before, after) == ["fakews 17.1 -> 15.0.1"]

    def test_a_removed_loaded_package_is_reported(self) -> None:
        assert stale_loaded_distributions({"fakews": "17.1"}, {}) == ["fakews 17.1 -> removed"]

    def test_a_package_this_process_never_imported_is_fine(self) -> None:
        before = {"fakews": "17.1", "not-loaded": "1.0"}
        after = {"fakews": "17.1", "not-loaded": "2.0"}

        assert stale_loaded_distributions(before, after) == []

    def test_new_packages_alone_need_no_restart(self) -> None:
        before = {"fakews": "17.1"}
        after = {"fakews": "17.1", "brand-new": "1.0"}

        assert stale_loaded_distributions(before, after) == []
