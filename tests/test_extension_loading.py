"""Loading extensions: a broken one is logged and skipped, never fatal.

Extensions are discovered as modules under `wactorz/ext/`. Each is set up in a
first pass, and the ones with an `on_ready` hook are registered to run at server
start in an order that respects their declared `__deps__` — dependencies first,
anything cyclic or undeclared after, in discovery order. The browser config and
per-actor decorators an extension offers are gathered the same forgiving way.
"""

import types
from typing import Any

import pytest
from aiohttp import web

from wactorz import ext


def _module(name: str, **attrs: Any) -> Any:
    module = types.ModuleType(f"wactorz.ext.{name}")
    module.__dict__.update(attrs)
    return module


def _use(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> None:
    monkeypatch.setattr(ext, "discover", lambda: list(modules))


async def _ready(_app: web.Application) -> None:
    return None


LOADED = web.AppKey("loaded", bool)


def _setup_ok(app: web.Application) -> None:
    app[LOADED] = True


def _setup_broken(_app: web.Application) -> None:
    raise RuntimeError("missing key")


class TestSetup:
    def test_a_failing_setup_does_not_stop_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use(monkeypatch, _module("broken", setup=_setup_broken), _module("fine", setup=_setup_ok))
        app = web.Application()

        ext.setup_all(app)

        assert app[LOADED] is True

    def test_on_ready_hooks_run_dependencies_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def graph(_app: web.Application) -> None: ...
        async def store(_app: web.Application) -> None: ...
        async def ui(_app: web.Application) -> None: ...

        modules = [
            _module("ui", setup=_setup_ok, on_ready=ui, __deps__=["graph", "absent"]),
            _module("graph", setup=_setup_ok, on_ready=graph, __deps__=["store"]),
            _module("store", setup=_setup_ok, on_ready=store),
            _module("plain", setup=_setup_ok),
        ]
        _use(monkeypatch, *modules)
        app = web.Application()

        ext.setup_all(app)

        assert list(app.on_startup)[-3:] == [ui, graph, store]

    def test_a_dependency_cycle_is_skipped_to_the_end(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        a = _module("a", on_ready=_ready, __deps__=["b"])
        b = _module("b", on_ready=_ready, __deps__=["a"])
        c = _module("c", on_ready=_ready)

        ordered = ext._topo_sort_on_ready([a, b, c])

        assert ordered == [c, a, b]
        assert "on_ready cycle detected among ['a', 'b']" in caplog.text

    def test_no_hooks_is_an_empty_order(self) -> None:
        assert ext._topo_sort_on_ready([_module("x", setup=_setup_ok)]) == []


class TestCollecting:
    def test_public_config_is_namespaced_and_failures_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _broken(_app: web.Application) -> dict[str, Any]:
            raise RuntimeError("no dataset")

        _use(
            monkeypatch,
            _module("fuseki", setup=_setup_ok, public_config=lambda _app: {"dataset": "wactorz"}),
            _module("graph", setup=_setup_ok, public_config=_broken),
            _module("quiet", setup=_setup_ok),
        )

        assert ext.collect_public_config(web.Application()) == {"fuseki": {"dataset": "wactorz"}}

    def test_actor_decorators_skip_failures_and_nones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _decorate(actor: Any, payload: dict[str, Any]) -> None:
            payload["did"] = "did:x"

        def _broken(_app: web.Application) -> Any:
            raise RuntimeError("index unavailable")

        _use(
            monkeypatch,
            _module("identity", setup=_setup_ok, actor_decorator=lambda _app: _decorate),
            _module("broken", setup=_setup_ok, actor_decorator=_broken),
            _module("none", setup=_setup_ok, actor_decorator=lambda _app: None),
            _module("quiet", setup=_setup_ok),
        )

        assert ext.collect_actor_decorators(web.Application()) == [_decorate]


class TestDiscovery:
    def test_real_extensions_are_found_and_a_broken_import_skipped(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        assert "wactorz.ext.tts" in [m.__name__ for m in ext.discover()]

        real_import = ext.importlib.import_module

        def _import(name: str) -> Any:
            if name == "wactorz.ext.tts":
                raise ImportError("a dependency is missing")
            return real_import(name)

        monkeypatch.setattr(ext.importlib, "import_module", _import)

        assert "wactorz.ext.tts" not in [m.__name__ for m in ext.discover()]
        assert "tts failed to import" in caplog.text
