"""The dashboard is told which Wactorz is answering it."""

from __future__ import annotations

import json

from aiohttp import web

import wactorz
from wactorz.web import api_system


class _Request:
    """Just enough of a request for the config handler."""

    def __init__(self) -> None:
        self.host = "localhost:8888"
        self.secure = False
        # The extensions contribute their own config through the app.
        self.app = web.Application()


async def _config() -> dict:
    response = await api_system.config_handler(_Request())  # type: ignore[arg-type]
    body = response.body
    assert isinstance(body, bytes)
    return json.loads(body)


class TestTheVersionTheBrowserIsTold:
    async def test_it_is_the_one_this_process_is(self) -> None:
        assert (await _config())["version"] == wactorz.__version__

    async def test_it_is_reported_at_all(self) -> None:
        # Read from the server rather than built into the bundle: `static/app`
        # is committed and can lag the wheel serving it, and a version baked in
        # at build time would name the wrong one exactly when someone is looking
        # to find out what they are running.
        assert (await _config())["version"]
