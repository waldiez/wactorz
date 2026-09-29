"""Serving the built dashboard and the docs, and starting the monitor server.

Assets are served only from inside their own directories — a path that climbs
out, or a sibling that merely shares a prefix, is a 404 — and behind Home
Assistant ingress the JavaScript bundle is rewritten so its absolute API paths
carry the ingress prefix. The docs resolve a directory to its index page.

Starting the server checks its preconditions first and says which failed, and a
port taken between the check and the bind is a clean exit rather than a
traceback.
"""

import asyncio
import socket
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from wactorz import config
from wactorz.web import app as web_app
from wactorz.web import mqtt, runtime, static_site


@pytest.fixture(name="site")
def site_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    dist, public, docs = tmp_path / "static" / "app", tmp_path / "public", tmp_path / "docs"
    (dist / "assets").mkdir(parents=True)
    public.mkdir()
    (docs / "guide").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<html><head><script>sw()</script></head></html>", encoding="utf-8"
    )
    (dist / "assets" / "main.js").write_text(
        'fetch("/api/actors");fetch("/config");fetch("/actors");new WebSocket(`ws://${location.host}/ws`)',
        encoding="utf-8",
    )
    (dist / "assets" / "logo.png").write_bytes(b"png")
    (tmp_path / "static" / "app-old").mkdir()
    (tmp_path / "static" / "app-old" / "secret.txt").write_text("secret", encoding="utf-8")
    (public / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (docs / "guide" / "index.html").write_text("guide", encoding="utf-8")
    (docs / "page.html").write_text("page", encoding="utf-8")
    monkeypatch.setattr(static_site, "FRONTEND_DIST", dist)
    monkeypatch.setattr(static_site, "FRONTEND_PUBLIC", public)
    monkeypatch.setattr(static_site, "DOCS_SITE", docs)
    monkeypatch.setattr(config, "INGRESS_ENABLED", False)
    return tmp_path


@pytest.fixture(name="client")
async def client_fixture(site: Path) -> AsyncIterator[TestClient[Any, Any]]:
    app = web.Application()
    app.router.add_get("/", static_site.index_handler)
    app.router.add_get("/favicon.svg", static_site.index_handler)
    app.router.add_get("/docs/", static_site.docs_handler)
    app.router.add_get("/docs/{path:.+}", static_site.docs_handler)
    app.router.add_get("/{path:.+}", static_site.static_handler)
    async with TestClient(TestServer(app)) as client:
        yield client


class TestDashboard:
    async def test_the_shell_carries_a_nonce_on_every_inline_script(
        self, client: TestClient[Any, Any]
    ) -> None:
        resp = await client.get("/")
        body = await resp.text()

        nonce = resp.headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        assert body.count(f"nonce='{nonce}'") == 2
        assert "<base href" not in body
        assert resp.headers["Cache-Control"].startswith("no-")

    async def test_behind_ingress_the_shell_is_based_on_the_prefix(
        self, client: TestClient[Any, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(config, "INGRESS_ENABLED", True)

        body = await (
            await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
        ).text()

        assert '<base href="/api/hassio_ingress/abc/">' in body

    async def test_the_favicon_comes_from_the_public_assets(
        self, client: TestClient[Any, Any]
    ) -> None:
        assert await (await client.get("/favicon.svg")).text() == "<svg/>"

    async def test_an_asset_is_served_and_the_bundle_rewritten_behind_ingress(
        self, client: TestClient[Any, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = await (await client.get("/assets/main.js")).text()
        monkeypatch.setattr(config, "INGRESS_ENABLED", True)
        rewritten = await (
            await client.get("/assets/main.js", headers={"X-Ingress-Path": "/ing"})
        ).text()

        assert '"/api/actors"' in plain
        assert (
            '"/ing/api/actors"' in rewritten
            and '"/ing/config"' in rewritten
            and '"/ing/actors"' in rewritten
        )
        assert f"location.hostname}}:{runtime.WS_PORT}/ws" in rewritten
        assert await (await client.get("/assets/logo.png")).read() == b"png"

    @pytest.mark.parametrize(
        "path", ["/../app-old/secret.txt", "/%2e%2e/app-old/secret.txt", "/missing.js"]
    )
    async def test_nothing_outside_the_asset_directory_is_served(
        self, client: TestClient[Any, Any], path: str
    ) -> None:
        assert (await client.get(path)).status == 404

    async def test_without_a_built_dashboard_the_shell_is_not_found(
        self, client: TestClient[Any, Any], site: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (site / "static" / "app" / "index.html").unlink()
        monkeypatch.setattr(static_site, "_find_dir", lambda *_rel: site / "nowhere")

        assert (await client.get("/")).status == 404

    async def test_a_favicon_missing_everywhere_is_the_shell(
        self, client: TestClient[Any, Any], site: Path
    ) -> None:
        (site / "public" / "favicon.svg").unlink()

        assert "<html>" in await (await client.get("/favicon.svg")).text()


class TestDocs:
    async def test_pages_and_directory_indexes(self, client: TestClient[Any, Any]) -> None:
        assert await (await client.get("/docs/page.html")).text() == "page"
        assert await (await client.get("/docs/guide/")).text() == "guide"

    async def test_a_missing_index_redirects_to_the_first_section(
        self, client: TestClient[Any, Any]
    ) -> None:
        resp = await client.get("/docs/", allow_redirects=False)

        assert (resp.status, resp.headers["Location"]) == (302, "/docs/guide/index.html")

    async def test_unknown_or_escaping_paths_are_not_found(
        self, client: TestClient[Any, Any]
    ) -> None:
        assert (await client.get("/docs/nope.html")).status == 404
        assert (await client.get("/docs/%2e%2e/public/favicon.svg")).status == 404

    async def test_unbuilt_docs_say_how_to_build_them(
        self, client: TestClient[Any, Any], site: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(static_site, "DOCS_SITE", site / "no-docs")

        resp = await client.get("/docs/")

        assert resp.status == 404 and "Docs not built" in (resp.reason or "")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(name="startup")
def startup_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every precondition passing, on a free local port, with no broker loop."""
    calls: dict[str, Any] = {"listened": 0}
    monkeypatch.setattr(config, "CONFIG", replace(config.CONFIG, bind_host="127.0.0.1", api_key=""))
    monkeypatch.setattr(web_app, "CONFIG", config.CONFIG)
    monkeypatch.setattr(runtime, "WS_PORT", _free_port())

    async def _ok() -> bool:
        return True

    async def _listen() -> None:
        calls["listened"] += 1

    async def _forever() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(mqtt, "check_mqtt", _ok)
    monkeypatch.setattr(web_app, "check_ws_port", _ok)
    monkeypatch.setattr(mqtt, "mqtt_listener", _listen)
    monkeypatch.setattr(web_app.ws, "totals_broadcaster", _forever)
    monkeypatch.setattr(web_app.log_stream, "log_push_loop", _forever)
    return calls


class TestStartup:
    async def test_a_ready_server_serves_then_releases_its_port(
        self, startup: dict[str, Any]
    ) -> None:
        await web_app.main()

        assert startup["listened"] == 1
        with socket.socket() as again:
            again.bind(("127.0.0.1", runtime.WS_PORT))  # released: the bind succeeds

    async def test_failed_preconditions_stop_the_start(
        self,
        startup: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def _no() -> bool:
            return False

        monkeypatch.setattr(mqtt, "check_mqtt", _no)
        monkeypatch.setattr(web_app, "check_ws_port", _no)

        await web_app.main()
        with pytest.raises(SystemExit):
            await web_app.main(exit_on_failure=True)

        assert "MQTT broker unreachable" in caplog.text and "already in use" in caplog.text
        assert startup["listened"] == 0

    async def test_an_exposed_server_without_a_key_refuses_to_start(
        self,
        startup: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            web_app, "CONFIG", replace(config.CONFIG, bind_host="0.0.0.0", api_key="short")
        )  # the exposure under test

        await web_app.main()

        assert "API_KEY is 5 characters" in caplog.text
        assert startup["listened"] == 1, "a key, however short, is not the refusal case"

        monkeypatch.setattr(
            web_app, "CONFIG", replace(config.CONFIG, bind_host="0.0.0.0", api_key="")
        )  # the exposure under test
        monkeypatch.delenv("WACTORZ_EXPOSED_OK", raising=False)
        with pytest.raises(SystemExit):
            await web_app.main(exit_on_failure=True)

    async def test_a_port_taken_after_the_check_is_a_clean_exit(
        self, startup: dict[str, Any]
    ) -> None:
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", runtime.WS_PORT))
            taken.listen()

            with pytest.raises(SystemExit):
                await web_app.main()

    async def test_the_port_check_answers_free_or_taken(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(web_app, "CONFIG", replace(config.CONFIG, bind_host="127.0.0.1"))
        monkeypatch.setattr(runtime, "WS_PORT", _free_port())

        assert await web_app.check_ws_port() is True
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", runtime.WS_PORT))
            taken.listen()
            assert await web_app.check_ws_port() is False


class TestConsoleScript:
    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_it_runs_main_and_exits_with_its_status(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        async def _fail(exit_on_failure: bool = False) -> None:
            assert exit_on_failure is True
            raise SystemExit(3)

        monkeypatch.setattr(web_app, "main", _fail)
        monkeypatch.setattr(web_app.sys, "platform", platform)

        with pytest.raises(SystemExit) as exited:
            web_app.cli_main()

        assert exited.value.code == 3
