"""The monitor's API key, and the rule that refuses to start without one.

 Two separate protections, and neither replaces the other. The key guards the
routes; the fail-closed rule guards the *configuration* — because a warning
about being reachable scrolls past in a container log while the operator
believes they merely changed an address.

 **A key means no dashboard, deliberately.** The browser holds no credential
until the cookie work lands, so a guarded install answers 401 at the door rather
than serving a page that loads and then fails every call it makes.
"""

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from wactorz.web import auth


def _request(headers: dict[str, str] | None = None, path: str = "/api/actors") -> Any:
    return make_mocked_request("GET", path, headers=headers or {})


class TestTheKeyCheck:
    def test_no_key_configured_leaves_everything_open(self) -> None:
        # The default install, protected by the loopback bind rather than a
        # credential.
        assert auth.is_authorized(_request(), "")

    def test_the_right_key_in_either_header(self) -> None:
        assert auth.is_authorized(_request({"X-API-Key": "s3cret"}), "s3cret")
        # Bearer too, so scrapers that only speak standard auth headers —
        # Prometheus among them — can reach a guarded endpoint.
        assert auth.is_authorized(_request({"Authorization": "Bearer s3cret"}), "s3cret")
        assert auth.is_authorized(_request({"Authorization": "bearer s3cret"}), "s3cret")

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"X-API-Key": "wrong"},
            {"X-API-Key": ""},
            {"X-API-Key": "s3cre"},  # a prefix must not pass on length alone
            {"Authorization": "Basic s3cret"},
            {"Authorization": "s3cret"},
        ],
    )
    def test_anything_else_is_refused(self, headers: dict[str, str]) -> None:
        assert not auth.is_authorized(_request(headers), "s3cret")


class TestWhatStaysOpen:
    def test_health_is_never_guarded(self) -> None:
        # Not a style choice: the container's HEALTHCHECK curls it with no
        # key, so guarding it makes every container report unhealthy — and
        # anything acting on that restarts a healthy process, in a loop.
        assert "/health" in auth.UNGUARDED_PATHS

    def test_the_dashboard_is_not_exempt(self) -> None:
        # The decision worth pinning: a key means an honest refusal at the door,
        # not a page that loads and then 401s on every call it makes.
        assert "/" not in auth.UNGUARDED_PATHS


class TestTheStartupRefusal:
    def test_loopback_needs_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(auth.EXPOSED_OK_ENV, raising=False)

        assert auth.exposure_refusal("127.0.0.1", "") is None

    def test_a_key_makes_a_wide_bind_acceptable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(auth.EXPOSED_OK_ENV, raising=False)

        assert auth.exposure_refusal("0.0.0.0", "s3cret") is None

    def test_reachable_with_no_key_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(auth.EXPOSED_OK_ENV, raising=False)

        refusal = auth.exposure_refusal("0.0.0.0", "")

        assert refusal is not None
        # It has to say all three ways out, or it is a dead end for whoever hits it.
        assert "API_KEY" in refusal
        assert "127.0.0.1" in refusal
        assert auth.EXPOSED_OK_ENV in refusal

    def test_the_escape_hatch_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # What both shipped deployments use: a container cannot see its own port
        # mappings, so its exposure has to be declared rather than inferred.
        monkeypatch.setenv(auth.EXPOSED_OK_ENV, "1")

        assert auth.exposure_refusal("0.0.0.0", "") is None

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "nonsense"])
    def test_only_a_real_yes_counts(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        # `WACTORZ_EXPOSED_OK=false` used to opt you *in*: the check accepted
        # anything but "" and "0". It shares `_env_truthy` with the rest of the
        # config now, so the vocabulary is the same one everywhere.
        monkeypatch.setenv(auth.EXPOSED_OK_ENV, value)

        assert auth.exposure_refusal("0.0.0.0", "") is not None

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_every_spelling_of_yes_opts_out(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(auth.EXPOSED_OK_ENV, value)

        assert auth.exposure_refusal("0.0.0.0", "") is None


class TestEverySeverIsCovered:
    """Three servers read `CONFIG.bind_host` — the monitor, the REST API and
    the WhatsApp webhook. A refusal that lived in the monitor's own startup left
    the REST interface serving chat and lifecycle commands to the network in
    exactly the configuration it exists to stop, while the monitor refused.
    """

    def test_the_refusal_runs_at_the_process_root(self) -> None:
        from pathlib import Path

        source = Path("wactorz/app.py").read_text(encoding="utf-8")

        assert "exposure_refusal(CONFIG.bind_host, CONFIG.api_key)" in source

    def test_the_monitor_keeps_its_own_check(self) -> None:
        # `wactorz-monitor` is its own console script and never goes through
        # `app.py`, so both entry points need it.
        from pathlib import Path

        source = Path("wactorz/web/app.py").read_text(encoding="utf-8")

        assert "exposure_refusal(CONFIG.bind_host, CONFIG.api_key)" in source


def _with_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    """Swap CONFIG for a copy carrying `key`.

    `CONFIG` is a frozen dataclass, so the field cannot be patched in place —
    and the middleware imports it inside the function, so replacing the module
    attribute is what it sees.
    """
    import dataclasses

    from wactorz import config

    monkeypatch.setattr(config, "CONFIG", dataclasses.replace(config.CONFIG, api_key=key))


class TestTheMiddleware:
    async def test_it_refuses_with_401_once_a_key_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_key(monkeypatch, "s3cret")

        async def _handler(_r: Any) -> web.Response:
            raise AssertionError("an unauthenticated request reached the handler")

        response = await auth.auth_middleware(_request(), _handler)

        assert response.status == 401

    async def test_health_still_reaches_its_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _with_key(monkeypatch, "s3cret")

        async def _handler(_r: Any) -> web.Response:
            return web.Response(text="ok")

        response = await auth.auth_middleware(_request(path="/health"), _handler)

        assert response.status == 200
