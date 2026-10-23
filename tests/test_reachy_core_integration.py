"""Strict recipe installation keeps the generic and security contracts intact."""

from pathlib import Path
from typing import Any

import pytest

from tests.test_catalog_agent import _Installer, _Main, _Registry, _spawner
from wactorz.agents import catalog_agent, dependency_install, installer_agent
from wactorz.agents.catalog_agent import CatalogAgent, _build_catalog
from wactorz.agents.installer_agent import InstallerAgent
from wactorz.core.actor import Message, MessageType


def test_strict_installation_is_only_declared_by_the_robot_recipe() -> None:
    catalog = _build_catalog()
    assert {name for name, recipe in catalog.items() if recipe.get("require_install_success")} == {
        "reachy-mini"
    }
    assert not catalog["anomaly-detector"].get("require_install_success", False)


async def test_python310_refuses_robot_before_any_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = CatalogAgent(persistence_dir=str(tmp_path))
    monkeypatch.setattr(catalog_agent.sys, "version_info", (3, 10, 0))
    result = await catalog._action_spawn("reachy-mini", {})
    assert not result["ok"]
    assert "requires Python 3.11+" in result["message"]
    assert "No packages were installed" in result["message"]


@pytest.mark.parametrize("strict", [False, True])
async def test_strict_installation_cannot_bypass_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, strict: bool
) -> None:
    installer = InstallerAgent(persistence_dir=str(tmp_path))
    cleared: list[list[str]] = []

    async def refuse(packages: list[str], _wanted_by: str) -> list[str]:
        cleared.append(packages)
        return packages

    async def no_pip(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        raise AssertionError("a refused requirement reached pip")

    monkeypatch.setattr(installer_agent, "requirement_is_satisfied", lambda _name: False)
    monkeypatch.setattr(installer, "_cleared", refuse)
    monkeypatch.setattr(installer, "_pip_install", no_pip)
    result = await installer._handle_install(
        Message(
            type=MessageType.TASK,
            sender_id="catalog-id",
            payload={
                "action": "install",
                "packages": ["wactorz-unlisted-probe==2"],
                "require_install_success": strict,
            },
        )
    )
    assert cleared == [["wactorz-unlisted-probe==2"]]
    assert result["refused"] == ["wactorz-unlisted-probe==2"]


@pytest.mark.parametrize("strict", [False, True])
async def test_strict_policy_cannot_authorize_restore_actions(tmp_path: Path, strict: bool) -> None:
    installer = InstallerAgent(persistence_dir=str(tmp_path))
    result = await installer._handle_install(
        Message(
            type=MessageType.TASK,
            sender_id="catalog-id",
            payload={"action": "install_restored", "require_install_success": strict},
        )
    )
    assert "only main" in result["error"]


@pytest.mark.parametrize("recipe", ["reachy-mini", "anomaly-detector"])
@pytest.mark.parametrize("failure", ["busy", "timeout", "restart", "refused"])
async def test_catalogue_failure_policies_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recipe: str, failure: str
) -> None:
    catalog = CatalogAgent(persistence_dir=str(tmp_path))
    # Registry persistence is covered separately; this test isolates spawning.
    monkeypatch.setattr(catalog_agent, "find_main_actor", lambda _registry: _Main(tmp_path))
    monkeypatch.setattr(catalog, "_mqtt_publish", _ignore_publish)
    monkeypatch.setattr(catalog, "notify_user", _ignore_publish)
    monkeypatch.setattr(catalog_agent, "missing_requirements", lambda _reqs: ["numpy"])
    monkeypatch.setattr(catalog_agent, "INSTALL_WAIT_S", 0.01)
    monkeypatch.setattr(dependency_install, "install_wait_s", lambda _count: 0.01)
    monkeypatch.setattr(dependency_install, "APPROVAL_TIMEOUT_S", 0)
    result: dict[str, Any] = {"success": True}
    if failure == "restart":
        result["restart_required"] = ["websockets 16 -> 15"]
    elif failure == "refused":
        result = {"error": "package refused", "refused": ["numpy"]}
    installer = _Installer(catalog, result, takes=failure != "busy", answers=failure != "timeout")
    catalog._registry = _Registry(installer)  # pyright: ignore[reportAttributeAccessIssue]
    spawner = _spawner(catalog, monkeypatch)
    answer = await catalog._action_spawn(recipe, {})
    blocked = recipe == "reachy-mini" or failure == "refused"
    assert bool(spawner.calls) is not blocked
    assert answer["ok"] is not blocked
    assert catalog._result_futures == {}


async def _ignore_publish(*_args: Any, **_kwargs: Any) -> None:
    pass
