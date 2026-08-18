"""Tests for version-aware catalog recipe dependencies."""

from unittest import mock

from wactorz.agents.catalog_agent import (
    _IMPORT_NAME_MAP,
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


def test_every_named_dependency_can_be_looked_up_by_import_name():
    """A distribution whose module has another name needs a mapping entry.

    Without one the check never finds the package, so a host with everything
    installed is sent to the installer on every spawn — the wait the fast path
    exists to avoid. `webrtcvad-wheels` imports as `webrtcvad`, and was missing.
    """
    import importlib.util

    for recipe in _build_catalog().values():
        for requirement in recipe.get("install", []):
            pip_name = requirement.split("==")[0].split("[")[0].strip().lower()
            import_name = _IMPORT_NAME_MAP.get(pip_name) or pip_name.replace("-", "_")
            if importlib.util.find_spec(import_name) is not None:
                continue
            # Not installed here, so only the obvious mismatches can be caught:
            # a name that differs from the dashed form must be mapped.
            assert pip_name.replace("-", "_") == import_name or pip_name in _IMPORT_NAME_MAP, (
                f"{requirement} resolves to import {import_name!r} with no mapping"
            )


def test_webrtcvad_wheels_is_recognised_when_installed():
    assert _IMPORT_NAME_MAP["webrtcvad-wheels"] == "webrtcvad"


def test_reachy_recipe_installs_the_speech_recogniser_it_defaults_to():
    """`ask_voice` and `conversation_start` transcribe with faster-whisper.

    It was not in the list, so every voice feature failed on a robot installed
    exactly as instructed, and nothing said so until the first attempt.
    """
    install = _build_catalog()["reachy-mini"]["install"]

    assert "faster-whisper" in install


def test_the_reachy_setup_docs_name_everything_the_recipe_installs():
    recipe = _build_catalog()["reachy-mini"]
    docs = recipe["docs"]

    for requirement in recipe["install"]:
        assert requirement.split("==")[0] in docs, f"{requirement} missing from setup docs"
