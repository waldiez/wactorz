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


class TestWhatBelongsInExt:
    def test_every_extension_imports_on_a_base_install(self) -> None:
        """A module under `wactorz/ext/` is imported on every start, so one that
        needs an optional library would warn on every start for everyone who
        does not have it. Such bridges live in `wactorz/core/integrations/` instead."""
        import importlib
        import pkgutil

        for module in pkgutil.iter_modules(ext.__path__):
            if module.name.startswith("_"):
                continue
            importlib.import_module(f"wactorz.ext.{module.name}")


class _EntryPoint:
    """One ``wactorz.extensions`` entry point, as importlib.metadata hands them out."""

    def __init__(self, name: str, value: str, loaded: Any = None, error: Exception | None = None):
        self.name = name
        self.value = value
        self._loaded = loaded
        self._error = error

    def load(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._loaded


class _Panel:
    """An extension that is an object rather than a module: ``module:object`` in the entry point."""

    def __init__(self) -> None:
        self.set_up = False

    def setup(self, app: web.Application) -> None:
        self.set_up = True

    def public_config(self, _app: web.Application) -> dict[str, Any]:
        return {"panel": True}


def _external(name: str, **attrs: Any) -> Any:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def _entry_points(monkeypatch: pytest.MonkeyPatch, *entries: _EntryPoint) -> None:
    monkeypatch.setattr(ext, "_entry_points", lambda: list(entries))
    monkeypatch.setattr(ext, "_external", None)
    monkeypatch.setattr(ext, "_entry_point_names", {})


class TestExtensionsFromOtherPackages:
    def test_an_installed_package_is_loaded_after_the_built_ins_under_its_entry_point_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mlops = _external(
            "wactorz_mlops.ext", setup=_setup_ok, public_config=lambda _app: {"designer": 1}
        )
        _entry_points(monkeypatch, _EntryPoint("mlops", "wactorz_mlops.ext", mlops))

        found = ext.discover(refresh=True)

        assert found[-1] is mlops
        assert [ext._short_name(m) for m in found[:-1]] == ["tts"]
        assert ext._short_name(mlops) == "mlops"
        app = web.Application()
        ext.setup_all(app)
        assert app[LOADED] is True
        assert ext.collect_public_config(app)["mlops"] == {"designer": 1}

    def test_an_object_with_setup_works_as_well_as_a_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        panel = _Panel()
        _entry_points(monkeypatch, _EntryPoint("panel", "wactorz_mlops.ext:panel", panel))

        found = ext.discover(refresh=True)

        assert found[-1] is panel
        app = web.Application()
        ext.setup_all(app)
        assert panel.set_up
        assert ext.collect_public_config(app)["panel"] == {"panel": True}

    def test_a_broken_or_hookless_entry_point_is_skipped_and_said(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _entry_points(
            monkeypatch,
            _EntryPoint("broken", "nope.ext", error=ImportError("no module named nope")),
            _EntryPoint("mute", "quiet.ext", _external("quiet.ext")),
            _EntryPoint("fine", "fine.ext", _external("fine.ext", setup=_setup_ok)),
        )

        with caplog.at_level("WARNING"):
            found = ext.discover(refresh=True)

        assert [ext._short_name(m) for m in found] == ["tts", "fine"]
        assert "broken = nope.ext: no module named nope" in caplog.text
        assert "mute = quiet.ext: it has no setup(app)" in caplog.text

    def test_a_name_a_built_in_uses_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A package must not stand in for a built-in by taking its name."""
        impostor = _external("evil.ext", setup=_setup_ok)
        _entry_points(monkeypatch, _EntryPoint("tts", "evil.ext", impostor))

        with caplog.at_level("WARNING"):
            found = ext.discover(refresh=True)

        assert impostor not in found
        assert "an extension called 'tts' is already loaded" in caplog.text

    def test_two_packages_with_one_name_keep_the_first(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        first = _external("one.ext", setup=_setup_ok)
        second = _external("two.ext", setup=_setup_ok)
        _entry_points(
            monkeypatch,
            _EntryPoint("mlops", "one.ext", first),
            _EntryPoint("mlops", "two.ext", second),
        )

        with caplog.at_level("WARNING"):
            found = ext.discover(refresh=True)

        assert first in found
        assert second not in found
        assert "two.ext" in caplog.text

    def test_the_entry_points_are_read_once_unless_asked_again(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reads: list[int] = []
        mlops = _external("wactorz_mlops.ext", setup=_setup_ok)

        def _read() -> list[_EntryPoint]:
            reads.append(1)
            return [_EntryPoint("mlops", "wactorz_mlops.ext", mlops)]

        monkeypatch.setattr(ext, "_entry_points", _read)
        monkeypatch.setattr(ext, "_external", None)
        monkeypatch.setattr(ext, "_entry_point_names", {})

        ext.discover()
        ext.discover()
        assert len(reads) == 1
        ext.discover(refresh=True)
        assert len(reads) == 2

    def test_deps_may_name_a_packaged_extension(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``__deps__`` refers to the entry point's name, as it does to a built-in's."""

        async def first(_app: web.Application) -> None: ...
        async def second(_app: web.Application) -> None: ...

        store = _external("wactorz_mlops.store", setup=_setup_ok, on_ready=first)
        ui = _external("wactorz_mlops.ui", setup=_setup_ok, on_ready=second, __deps__=["store"])
        _entry_points(
            monkeypatch,
            _EntryPoint("ui", "wactorz_mlops.ui", ui),
            _EntryPoint("store", "wactorz_mlops.store", store),
        )
        ext.discover(refresh=True)

        assert ext._topo_sort_on_ready([ui, store]) == [ui, store]
