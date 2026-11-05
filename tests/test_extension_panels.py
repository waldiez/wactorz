"""A dashboard tab from an extension: what it declares, and what the server makes of it.

A panel's script runs in the page with the signed-in session, so the server lists
and serves one only for an extension whose ``setup()`` succeeded, and refuses a
declaration that would take a built-in view's place or point at nothing.
"""

import dataclasses
import logging
import types
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz import config, ext
from wactorz.ext import PANEL_ICONS, PANELS, Panel
from wactorz.ext._panel import ICON_MAX_BYTES
from wactorz.web import relay, runtime
from wactorz.web.app import build_app

KEY = "a-key-for-the-panel-tests"
SCRIPT = "export const apiVersion = 1;\nexport function mount(root) { root.textContent = 'hi'; }\n"
ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M9 2v5"/></svg>\n'


def _module(name: str, **attrs: Any) -> Any:
    module = types.ModuleType(f"wactorz_test_{name}")
    module.__dict__.update({"setup": lambda _app: None, **attrs})
    return module


def _use(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> None:
    monkeypatch.setattr(ext, "discover", lambda: list(modules))
    monkeypatch.setattr(
        ext, "_entry_point_names", {m.__name__: m.__name__.split("_")[-1] for m in modules}
    )


@pytest.fixture(name="script")
def script_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "panel.js"
    # Bytes, so Windows writes no "\r\n": the server serves the file as it is.
    path.write_bytes(SCRIPT.encode())
    return path


@pytest.fixture(autouse=True)
def _nothing_followed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(relay, "_followed", ())
    yield


def _loaded(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> web.Application:
    _use(monkeypatch, *modules)
    app = web.Application()
    ext.setup_all(app)
    return app


def _panels(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> dict[str, Panel]:
    return _loaded(monkeypatch, *modules)[PANELS]


class TestWhatIsTaken:
    def test_a_loaded_extensions_panel(self, monkeypatch: pytest.MonkeyPatch, script: Path) -> None:
        panel = Panel(title="MLOps", script=script)

        assert _panels(monkeypatch, _module("mlops", panel=lambda _app: panel)) == {"mlops": panel}

    def test_none_without_the_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _panels(monkeypatch, _module("plain")) == {}

    def test_none_for_an_extension_whose_setup_failed(
        self, monkeypatch: pytest.MonkeyPatch, script: Path
    ) -> None:
        def broken(_app: web.Application) -> None:
            raise RuntimeError("no database")

        module = _module("broken", setup=broken, panel=lambda _app: Panel("Broken", script))

        assert _panels(monkeypatch, module) == {}

    def test_none_when_the_hook_fails_and_the_extension_stays_loaded(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        def broken(_app: web.Application) -> Panel:
            raise RuntimeError("no script built")

        with caplog.at_level(logging.WARNING, logger="wactorz.ext"):
            assert _panels(monkeypatch, _module("half", panel=broken)) == {}

        assert "panel() failed" in caplog.text
        assert "setup failed" not in caplog.text

    @pytest.mark.parametrize(
        ("name", "title", "script_name", "said"),
        [
            ("models", "Models", "panel.js", "the dashboard has a view of that name"),
            ("Bad", "Bad", "panel.js", "cannot be a dashboard address"),
            ("blank", " ", "panel.js", "no title"),
            ("gone", "Gone", "missing.js", "is not a .js file"),
            ("styled", "Styled", "panel.css", "is not a .js file"),
        ],
    )
    def test_a_panel_that_cannot_be_shown_is_refused_with_a_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
        name: str,
        title: str,
        script_name: str,
        said: str,
    ) -> None:
        (tmp_path / "panel.css").write_text("")
        (tmp_path / "panel.js").write_text(SCRIPT)
        module = _module(name, panel=lambda _app: Panel(title, tmp_path / script_name))

        with caplog.at_level(logging.WARNING, logger="wactorz.ext"):
            assert _panels(monkeypatch, module) == {}

        assert said in caplog.text


class TestIconFiles:
    def test_a_panels_own_icon_is_read_once_when_it_loads(
        self, monkeypatch: pytest.MonkeyPatch, script: Path, tmp_path: Path
    ) -> None:
        icon = tmp_path / "icon.svg"
        icon.write_bytes(ICON.encode())
        panel = Panel(title="Energy", script=script, icon=icon)

        app = _loaded(monkeypatch, _module("energy", panel=lambda _app: panel))

        assert app[PANELS] == {"energy": panel}
        assert app[PANEL_ICONS] == {"energy": ICON}

    def test_an_icon_by_name_reads_no_file(
        self, monkeypatch: pytest.MonkeyPatch, script: Path
    ) -> None:
        panel = Panel(title="MLOps", script=script, icon="models")

        assert _loaded(monkeypatch, _module("mlops", panel=lambda _app: panel))[PANEL_ICONS] == {}

    @pytest.mark.parametrize(
        ("file_name", "content", "said"),
        [
            ("missing.svg", None, "is not an .svg file"),
            ("icon.png", ICON.encode(), "is not an .svg file"),
            ("big.svg", b"<svg>" + b" " * ICON_MAX_BYTES + b"</svg>", "larger than"),
            ("latin.svg", b"<svg>\xff</svg>", "cannot be read"),
            ("blank.svg", b"<p>not a drawing</p>", "no <svg> element"),
        ],
    )
    def test_an_icon_that_cannot_be_used_leaves_the_tab_on_the_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        script: Path,
        tmp_path: Path,
        file_name: str,
        content: bytes | None,
        said: str,
    ) -> None:
        icon = tmp_path / file_name
        if content is not None:
            icon.write_bytes(content)
        panel = Panel(title="Energy", script=script, icon=icon)

        with caplog.at_level(logging.WARNING, logger="wactorz.ext"):
            app = _loaded(monkeypatch, _module("energy", panel=lambda _app: panel))

        assert app[PANELS] == {"energy": panel}
        assert app[PANEL_ICONS] == {}
        assert "default icon" in caplog.text
        assert said in caplog.text


class TestTopics:
    def test_followed_topics_are_relayed(self) -> None:
        relay.follow(["mlops/+/state", "mlops/+/state"])

        assert relay.followed() == ("mlops/+/state",)
        assert relay.is_relayed("mlops/hvac/state")
        assert not relay.is_relayed("mlops/hvac/other")

    def test_following_again_replaces(self) -> None:
        relay.follow(["a/#"])
        relay.follow([])

        assert not relay.is_relayed("a/b")


@pytest.fixture(name="client")
async def client_fixture(
    monkeypatch: pytest.MonkeyPatch, script: Path
) -> AsyncIterator[TestClient]:
    monkeypatch.setattr(config, "CONFIG", dataclasses.replace(config.CONFIG, api_key=KEY))
    monkeypatch.setattr(runtime, "registry", None)
    panel = Panel(title="MLOps", script=script, icon="models", topics=("mlops/#",))
    icon = script.with_name("energy.svg")
    icon.write_bytes(ICON.encode())
    energy = Panel(title="Energy", script=script, icon=icon)
    _use(
        monkeypatch,
        _module("mlops", panel=lambda _app: panel),
        _module("energy", panel=lambda _app: energy),
        _module("plain"),
    )
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    yield client
    await client.close()


def _keyed() -> dict[str, str]:
    return {"X-API-Key": KEY}


class TestTheServer:
    async def test_lists_each_panel_for_the_page(self, client: TestClient) -> None:
        response = await client.get("/api/config", headers=_keyed())
        listed = (await response.json())["extensions"]["panels"]

        assert listed == [
            {"name": "mlops", "title": "MLOps", "icon": "models"},
            # A page that does not read the file still has an icon to show.
            {"name": "energy", "title": "Energy", "icon": "grid", "icon_svg": ICON},
        ]

    async def test_serves_the_script_as_a_module_that_is_checked_each_time(
        self, client: TestClient
    ) -> None:
        response = await client.get("/api/ext/mlops/panel.js", headers=_keyed())

        assert response.status == 200
        assert await response.text() == SCRIPT
        assert response.headers["Content-Type"].startswith("text/javascript")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-cache"

    @pytest.mark.parametrize("name", ["plain", "nobody", "..%2F..%2Fetc"])
    async def test_serves_nothing_for_an_extension_without_a_panel(
        self, client: TestClient, name: str
    ) -> None:
        response = await client.get(f"/api/ext/{name}/panel.js", headers=_keyed())

        assert response.status == 404

    async def test_asks_for_the_key(self, client: TestClient) -> None:
        assert (await client.get("/api/ext/mlops/panel.js")).status == 401

    async def test_relays_the_topics_a_panel_declared(self, client: TestClient) -> None:
        assert relay.followed() == ("mlops/#",)

    async def test_a_script_removed_since_start_is_not_found(
        self, client: TestClient, script: Path
    ) -> None:
        script.unlink()

        assert (await client.get("/api/ext/mlops/panel.js", headers=_keyed())).status == 404
