"""Which interface each server listens on is a setting, not a constant.

`WACTORZ_BIND_HOST` decides where the dashboard, the REST API and the WhatsApp
webhook listen. Without it an install on a laptop or a Pi serves all three to the
whole network whether or not anyone meant it to.

**The default is `0.0.0.0`, and that is deliberate.** A container publishes its
own ports and a process cannot see its own port mappings, so a process bound to
loopback inside one is unreachable through them — a tighter default here would
make every containerised install unreachable, and would make the change
impossible to cherry-pick into a patch. Tightening it belongs with a release that
documents the break. The shipped deployments set the variable explicitly, so that
flip is a no-op for them.
"""

import os
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from wactorz.config import CONFIG, AppConfig, _bind_host
from wactorz.interfaces.chat import rest, whatsapp
from wactorz.web import app


def _cfg(host: str) -> AppConfig:
    return replace(CONFIG, bind_host=host)


def _server() -> Any:
    """A listening server shaped like asyncio's: close() is sync, wait_closed is not.

    An AsyncMock here makes close() return a coroutine nobody awaits — a
    RuntimeWarning rather than a failure, so the test passes while leaking.
    """
    server = MagicMock()
    server.wait_closed = AsyncMock()
    return server


def _site_factory() -> MagicMock:
    """Stands in for web.TCPSite; records the address it was asked to bind."""
    return MagicMock(return_value=AsyncMock())


class TestTheSetting:
    def test_it_defaults_to_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Changed in 0.6.0, and breaking on purpose. The old default was
        # `0.0.0.0`, which served the dashboard and the whole API to the LAN out
        # of the box on an install that had asked for neither.
        monkeypatch.delenv("WACTORZ_BIND_HOST", raising=False)

        assert _bind_host() == "127.0.0.1"

    def test_an_operator_can_still_ask_for_every_interface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What a container needs: a published port cannot reach a process bound
        # to the container's own loopback.
        monkeypatch.setenv("WACTORZ_BIND_HOST", "0.0.0.0")

        assert _bind_host() == "0.0.0.0"

    def test_surrounding_whitespace_does_not_become_part_of_the_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stray space in a compose file or run.sh export would otherwise fail
        # the bind at startup with a message about the address, not the config.
        monkeypatch.setenv("WACTORZ_BIND_HOST", "  127.0.0.1 ")

        assert _bind_host() == "127.0.0.1"


class TestTheDashboardServer:
    async def test_it_serves_where_the_setting_says(self) -> None:
        site = _site_factory()
        with (
            patch.object(app, "CONFIG", _cfg("127.0.0.1")),
            patch.object(app.web, "TCPSite", site),
            patch.object(app.web, "AppRunner", MagicMock(return_value=AsyncMock())),
            patch.object(app, "build_app", MagicMock()),
            patch.object(app.mqtt, "check_mqtt", AsyncMock(return_value=True)),
            patch.object(app, "check_ws_port", AsyncMock(return_value=True)),
            patch.object(app.mqtt, "mqtt_listener", AsyncMock()),
            patch.object(app.ws, "totals_broadcaster", AsyncMock()),
        ):
            await app.main()

        assert site.call_args.args[1] == "127.0.0.1"

    async def test_the_port_probe_checks_the_interface_it_will_serve_on(self) -> None:
        # The probe and the serving socket must agree. Probing 0.0.0.0 while
        # serving loopback tests the wrong interface: the port can be busy on an
        # address nothing will listen on, or free on one that is already taken.
        opened = AsyncMock(return_value=_server())
        with (
            patch.object(app, "CONFIG", _cfg("127.0.0.1")),
            patch.object(app.asyncio, "start_server", opened),
        ):
            await app.check_ws_port()
        assert opened.await_args
        assert opened.await_args.args[1] == "127.0.0.1"


class TestTheRestApi:
    async def test_it_serves_where_the_setting_says(self) -> None:
        site = _site_factory()
        interface: Any = rest.RESTInterface.__new__(rest.RESTInterface)
        interface.port = 8000
        with (
            patch.object(rest, "CONFIG", _cfg("127.0.0.1")),
            patch.object(rest.web, "TCPSite", site),
            patch.object(rest.web, "AppRunner", MagicMock(return_value=AsyncMock())),
            patch.object(rest.RESTInterface, "build_app", MagicMock()),
            patch.object(rest.asyncio, "Event", MagicMock(return_value=AsyncMock())),
        ):
            await interface.run()

        assert site.call_args.args[1] == "127.0.0.1"

    async def test_it_reports_the_address_it_actually_bound(self, caplog: Any) -> None:
        # The address in the log is the one to reach the API on. A hardcoded
        # one advertises an interface the server may not be listening on.
        interface: Any = rest.RESTInterface.__new__(rest.RESTInterface)
        interface.port = 8000
        with (
            patch.object(rest, "CONFIG", _cfg("127.0.0.1")),
            patch.object(rest.web, "TCPSite", _site_factory()),
            patch.object(rest.web, "AppRunner", MagicMock(return_value=AsyncMock())),
            patch.object(rest.RESTInterface, "build_app", MagicMock()),
            patch.object(rest.asyncio, "Event", MagicMock(return_value=AsyncMock())),
            caplog.at_level("INFO"),
        ):
            await interface.run()

        assert "127.0.0.1" in caplog.text
        assert "0.0.0.0" not in caplog.text


class TestTheWhatsappWebhook:
    async def test_it_serves_where_the_setting_says(self) -> None:
        site = _site_factory()
        interface: Any = whatsapp.WhatsAppInterface.__new__(whatsapp.WhatsAppInterface)
        interface.port = 8080
        interface.allowed_numbers = ["+306900000000"]  # else run() refuses to start
        with (
            patch.object(whatsapp.WhatsAppInterface, "build_app", MagicMock()),
            patch.object(whatsapp, "CONFIG", _cfg("127.0.0.1")),
            patch.object(whatsapp.web, "TCPSite", site),
            patch.object(whatsapp.web, "AppRunner", MagicMock(return_value=AsyncMock())),
            patch.object(whatsapp.asyncio, "Event", MagicMock(return_value=AsyncMock())),
        ):
            await interface.run()

        assert site.call_args.args[1] == "127.0.0.1"


class TestTheShippedDeployments:
    """Containers set the variable explicitly, so tightening the default later
    is a no-op for them rather than an outage."""

    @pytest.mark.parametrize(
        "path",
        [
            "compose.yaml",
            "ha-addon/wactorz/run.sh",
            "ha-addon/wactorz-ultra/run.sh",
        ],
    )
    def test_it_declares_its_own_bind_host(self, path: str) -> None:
        # A container's ports are published from outside; a process bound to
        # loopback inside one is unreachable through them.
        assert "WACTORZ_BIND_HOST" in _repo_text(path)

    @pytest.mark.parametrize("path", ["ha-addon/wactorz/run.sh", "ha-addon/wactorz-ultra/run.sh"])
    def test_the_addons_declare_that_they_sit_behind_ingress(self, path: str) -> None:
        # Without it the panel has no way past the origin and host checks, since
        # the bypass does not exist unless a deployment claims it.
        assert "WACTORZ_INGRESS=1" in _repo_text(path)


def _repo_text(relative: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, relative), encoding="utf-8") as handle:
        return handle.read()
