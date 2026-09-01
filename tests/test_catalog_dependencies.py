"""Tests for version-aware catalog recipe dependencies."""

from unittest import mock

from wactorz.agents.catalog_agent import _IMPORT_NAME_MAP, _dependency_is_satisfied


def test_exact_dependency_requires_matching_installed_version():
    with (
        mock.patch(
            "wactorz.agents.catalog_agent.importlib.metadata.version",
            return_value="1.8.0",
        ),
        mock.patch("wactorz.agents.catalog_agent.importlib.import_module"),
    ):
        assert not _dependency_is_satisfied("example-package==1.8.4")


def test_exact_dependency_accepts_matching_installed_version():
    with (
        mock.patch(
            "wactorz.agents.catalog_agent.importlib.metadata.version",
            return_value="1.8.4",
        ),
        mock.patch("wactorz.agents.catalog_agent.importlib.import_module"),
    ):
        assert _dependency_is_satisfied("example-package==1.8.4")


def test_unversioned_dependency_only_needs_to_import():
    with (
        mock.patch("wactorz.agents.catalog_agent.importlib.import_module"),
        mock.patch("wactorz.agents.catalog_agent.importlib.metadata.version") as version,
    ):
        assert _dependency_is_satisfied("numpy")

    version.assert_not_called()


def test_distribution_with_different_import_name_is_mapped():
    assert _IMPORT_NAME_MAP["webrtcvad-wheels"] == "webrtcvad"
    assert _IMPORT_NAME_MAP["deepgram-sdk"] == "deepgram"
