"""Tests for version-aware catalog recipe dependencies."""

from unittest import mock

from wactorz.agents.catalog_agent import (
    _REACHY_MINI_REQUIREMENT,
    _build_catalog,
    _dependency_is_satisfied,
)


def test_reachy_recipe_pins_sdk_to_supported_daemon_version():
    recipe = _build_catalog()["reachy-mini"]

    assert _REACHY_MINI_REQUIREMENT == "reachy-mini==1.8.4"
    assert recipe["install"][0] == _REACHY_MINI_REQUIREMENT


def test_exact_dependency_requires_matching_installed_version():
    with (
        mock.patch(
            "wactorz.agents.catalog_agent.importlib.metadata.version",
            return_value="1.8.0",
        ),
        mock.patch("wactorz.agents.catalog_agent.importlib.import_module"),
    ):
        assert not _dependency_is_satisfied("reachy-mini==1.8.4")


def test_exact_dependency_accepts_matching_installed_version():
    with (
        mock.patch(
            "wactorz.agents.catalog_agent.importlib.metadata.version",
            return_value="1.8.4",
        ),
        mock.patch("wactorz.agents.catalog_agent.importlib.import_module"),
    ):
        assert _dependency_is_satisfied("reachy-mini==1.8.4")


def test_unversioned_dependency_only_needs_to_import():
    with (
        mock.patch("wactorz.agents.catalog_agent.importlib.import_module"),
        mock.patch("wactorz.agents.catalog_agent.importlib.metadata.version") as version,
    ):
        assert _dependency_is_satisfied("numpy")

    version.assert_not_called()
