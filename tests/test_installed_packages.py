"""Installed packages: only those named in WACTORZ_EXTENSIONS are loaded.

The fixture installs a real distribution in a folder on the path, whose module
says when it is imported, so "left out" means its code never ran, and the three
loaders are checked over the real metadata rather than a fake list.
"""

import dataclasses
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from wactorz import config as config_module
from wactorz import ext, installed, pipelines, plugins
from wactorz.agents.main.spawns import why_a_node_cannot_run
from wactorz.core.node_env import node_env_text
from wactorz.web import api_system

DISTRIBUTION = "Acme_Sensors"
MODULE = "acme_sensors_for_test"
SOURCE = f"""
import builtins
builtins.{MODULE}_imported = True
from wactorz import agent, pipeline

@agent()
def acme_detect(reading):
    return reading

class Panel:
    @staticmethod
    def setup(app):
        pass

panel = Panel()
flow = pipeline("acme-flow", [acme_detect])
"""
ENTRY_POINTS = f"""
[wactorz.agents]
acme = {MODULE}:acme_detect

[wactorz.extensions]
acme = {MODULE}:panel

[wactorz.pipelines]
acme = {MODULE}:flow
"""


def _imported() -> bool:
    return bool(getattr(sys.modules["builtins"], f"{MODULE}_imported", False))


def _allow(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(
        installed, "CONFIG", dataclasses.replace(installed.CONFIG, extensions_env=value)
    )


def _reset_loaders() -> None:
    plugins.clear()
    pipelines.clear()
    ext._external = None
    ext._entry_point_names.clear()
    installed.forget()


@pytest.fixture(autouse=True)
def distribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    (tmp_path / f"{MODULE}.py").write_text(SOURCE, encoding="utf-8")
    info = tmp_path / f"{DISTRIBUTION}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: 1.0\n", encoding="utf-8"
    )
    (info / "entry_points.txt").write_text(ENTRY_POINTS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    _reset_loaders()
    yield
    _reset_loaders()
    sys.modules.pop(MODULE, None)
    if _imported():
        delattr(sys.modules["builtins"], f"{MODULE}_imported")


class TestLeftOut:
    def test_an_unlisted_package_is_never_imported_by_any_loader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch, "")

        found_agents = plugins.discover(refresh=True)
        found_pipelines = pipelines.discover(refresh=True)
        extensions = ext.discover(refresh=True)

        assert "acme-detect" not in found_agents
        assert "acme-flow" not in found_pipelines
        assert not [m for m in extensions if MODULE in repr(m)]
        assert not _imported()

    def test_it_is_warned_about_once_with_the_line_to_add(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _allow(monkeypatch, "wactorz-mlops")

        with caplog.at_level(logging.WARNING, logger=installed.__name__):
            plugins.discover(refresh=True)
            pipelines.discover(refresh=True)
            ext.discover(refresh=True)

        lines = [r.getMessage() for r in caplog.records if DISTRIBUTION in r.getMessage()]
        assert len(lines) == 1
        assert f"WACTORZ_EXTENSIONS=wactorz-mlops,{DISTRIBUTION}" in lines[0]

    def test_a_package_installed_later_is_gated_on_a_refresh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # What an agent's requirements do: the package arrives while main runs.
        _allow(monkeypatch, "")
        registered = tmp_path / f"{DISTRIBUTION}-1.0.dist-info" / "entry_points.txt"
        registered.unlink()
        plugins.discover(refresh=True)
        assert installed.blocked() == []

        registered.write_text(ENTRY_POINTS, encoding="utf-8")

        assert "acme-detect" not in plugins.discover(refresh=True)
        assert installed.blocked() == [DISTRIBUTION]
        assert not _imported()

    def test_an_entry_point_with_no_distribution_is_left_out_by_its_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unknown = SimpleNamespace(value="mystery.mod:thing", dist=None)
        monkeypatch.setattr(installed.importlib.metadata, "entry_points", lambda group: [unknown])

        assert installed.entry_points("wactorz.agents", value="wactorz-mlops") == []
        assert installed.blocked() == ["mystery.mod:thing"]

    def test_targets_a_node_reports_leave_it_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A node builds what `targets` names, so the same list decides there.
        _allow(monkeypatch, "")
        plugins.discover(refresh=True)

        assert not [t for t in plugins.targets() if MODULE in t]


class TestLoaded:
    @pytest.mark.parametrize(
        "value", ["Acme_Sensors", "acme-sensors", "ACME.SENSORS", "other, acme_sensors", "*"]
    )
    def test_a_listed_package_is_loaded_in_every_group(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        _allow(monkeypatch, value)

        assert "acme-detect" in plugins.discover(refresh=True)
        assert "acme-flow" in pipelines.discover(refresh=True)
        assert any("Panel" in repr(m) for m in ext.discover(refresh=True))
        assert installed.blocked() == []

    def test_a_target_named_in_wactorz_agents_needs_no_listing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch, "")

        found = plugins.discover(env=f"{MODULE}:acme_detect", refresh=True)

        assert "acme-detect" in found

    def test_the_packages_own_agents_need_no_listing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _allow(monkeypatch, "")

        assert "wactorz.agents.recorder_agent:RecorderAgent" in plugins.targets()


class TestTellingTheDashboard:
    async def test_the_config_names_what_was_left_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _allow(monkeypatch, "")
        plugins.discover(refresh=True)
        monkeypatch.setattr(ext, "collect_public_config", lambda _app: {})

        response = await api_system.config_handler(make_mocked_request("GET", "/api/config"))

        assert f'"blocked": ["{DISTRIBUTION}"]' in str(response.text)


class TestANodesList:
    def _env(self, extensions: str) -> str:
        return node_env_text(
            "edge", "broker", 1883, username="", password="", extensions=extensions
        )

    def test_a_targets_list_reaches_the_nodes_env_quoted(self) -> None:
        # `*` unquoted would be a glob on the path that sources this file.
        assert "WACTORZ_EXTENSIONS='*'\n" in self._env("*")

    def test_unset_the_node_is_told_nothing(self) -> None:
        assert "WACTORZ_EXTENSIONS" not in self._env("")

    def test_the_target_reads_its_own_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEPLOY_TARGETS", "rpi-kitchen,rpi-garage")
        monkeypatch.setenv("DEPLOY_RPI_KITCHEN_EXTENSIONS", " wactorz-mlops ")
        monkeypatch.delenv("DEPLOY_RPI_GARAGE_EXTENSIONS", raising=False)

        kitchen, garage = config_module._deploy_targets()

        assert (kitchen.extensions, garage.extensions) == ("wactorz-mlops", "")


def test_the_advice_on_a_target_a_node_lacks_names_the_setting() -> None:
    # Following advice that left the setting out would change nothing.
    reason = why_a_node_cannot_run({"type": "module", "target": "acme.mod:agent"})

    assert reason is not None
    assert "WACTORZ_EXTENSIONS" in reason
