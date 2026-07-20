"""Tests for version-aware catalog recipe dependencies."""

from unittest import mock

from wactorz.agents.catalog_agent import (
    _REACHY_MINI_REQUIREMENT,
    CatalogAgent,
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


async def test_natural_spawn_requests_do_not_degrade_to_catalog_list():
    catalog = CatalogAgent(name="catalog-test")
    catalog._catalog = {
        "weather-agent": {
            "name": "weather-agent",
            "description": "Weather",
        }
    }
    catalog._action_spawn = mock.AsyncMock(
        return_value={"ok": True, "message": "'weather-agent' spawned and running"}
    )

    for request in (
        "Can you spawn weather agents from catalogs?",
        "Spawn weather agent from Catalog",
        "Tell Catalog Agent to Spawn Weather Agent",
    ):
        result = await catalog._handle(request)
        assert result["ok"] is True

    assert [call.args[0] for call in catalog._action_spawn.await_args_list] == [
        "weather-agent",
        "weather-agent",
        "weather-agent",
    ]
