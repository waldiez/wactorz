"""Tests for the extension discovery seam (wactorz.ext).

Discovery is sandboxed by pointing the package ``__path__`` at a tmp dir of
fake extension modules — nothing touches the real extensions, and the fakes'
``setup`` takes a plain recorder object, so no aiohttp is needed.
"""

# pylint: disable=missing-function-docstring

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from wactorz import ext


def _purge_fake_submodules() -> None:
    for name in list(sys.modules):
        if name.startswith("wactorz.ext."):
            del sys.modules[name]


@pytest.fixture(name="sandbox")
def sandbox_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect discovery to a tmp dir of fake extensions; clean re-imports."""

    def write(name: str, body: str) -> None:
        (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")

    _purge_fake_submodules()
    monkeypatch.setattr(ext, "__path__", [str(tmp_path)])
    yield write
    _purge_fake_submodules()


def test_extension_with_only_setup_is_discovered(sandbox) -> None:
    # Regression guard: setup alone is the whole required contract — discovery
    # must not demand any other attribute (e.g. a teardown hook).
    sandbox("solo", "def setup(app):\n    app.calls.append('solo')\n")
    names = [m.__name__.rsplit(".", 1)[-1] for m in ext.discover()]
    assert names == ["solo"]


def test_module_without_setup_is_not_an_extension(sandbox) -> None:
    sandbox("plain", "X = 1\n")
    assert not ext.discover()


def test_private_modules_are_skipped(sandbox) -> None:
    sandbox("_private", "def setup(app):\n    app.calls.append('private')\n")
    assert not ext.discover()


def test_broken_import_is_logged_and_skipped(sandbox, caplog) -> None:
    sandbox("broken", "raise RuntimeError('boom at import')\n")
    sandbox("good", "def setup(app):\n    app.calls.append('good')\n")
    with caplog.at_level("WARNING", logger="wactorz.ext"):
        names = [m.__name__.rsplit(".", 1)[-1] for m in ext.discover()]
    assert names == ["good"]
    assert "broken" in caplog.text and "failed to import" in caplog.text


def test_setup_all_runs_every_extension(sandbox) -> None:
    sandbox("alpha", "def setup(app):\n    app.calls.append('alpha')\n")
    sandbox("beta", "def setup(app):\n    app.calls.append('beta')\n")
    app = SimpleNamespace(calls=[])
    ext.setup_all(app)  # pyright: ignore[reportArgumentType]
    assert sorted(app.calls) == ["alpha", "beta"]


def test_setup_all_survives_a_raising_extension(sandbox, caplog) -> None:
    # A broken extension must never take the server down — and must not stop
    # the extensions after it from loading.
    sandbox("angry", "def setup(app):\n    raise RuntimeError('boom in setup')\n")
    sandbox("calm", "def setup(app):\n    app.calls.append('calm')\n")
    app = SimpleNamespace(calls=[])
    with caplog.at_level("WARNING", logger="wactorz.ext"):
        ext.setup_all(app)  # pyright: ignore[reportArgumentType]
    assert app.calls == ["calm"]
    assert "angry" in caplog.text and "setup failed" in caplog.text


def test_real_package_discovery_is_well_formed() -> None:
    # Against the real ext dir (empty today, populated later): every discovered
    # extension satisfies the contract.
    mods = ext.discover()
    assert all(callable(getattr(m, "setup", None)) for m in mods)
