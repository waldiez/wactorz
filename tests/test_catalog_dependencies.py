"""Version and import-name regression checks for catalogue dependencies."""

from unittest import mock

from wactorz.core.pip import IMPORT_NAMES, requirement_is_satisfied


def test_exact_dependency_requires_matching_installed_version():
    with mock.patch("wactorz.core.pip.importlib.metadata.version", return_value="1.8.0"):
        assert not requirement_is_satisfied("example-package==1.8.4")


def test_exact_dependency_accepts_matching_installed_version():
    with mock.patch("wactorz.core.pip.importlib.metadata.version", return_value="1.8.4"):
        assert requirement_is_satisfied("example-package==1.8.4")


def test_unversioned_dependency_needs_no_import_when_metadata_is_available():
    with (
        mock.patch("wactorz.core.pip.importlib.metadata.version", return_value="2.0"),
        mock.patch("wactorz.core.pip.importlib.import_module") as imported,
    ):
        assert requirement_is_satisfied("numpy")
    imported.assert_not_called()


def test_distribution_with_different_import_name_is_mapped():
    assert IMPORT_NAMES["webrtcvad-wheels"] == "webrtcvad"
