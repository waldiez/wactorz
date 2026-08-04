"""End-to-end: an extension's actor_decorator enriches the /api/actors payload.

Proves the seam is wired through the real handler — the monitor applies whatever
decorators extensions register, without knowing what they add.
"""

# pylint: disable=missing-function-docstring

import json
import sys
from collections.abc import Generator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from aiohttp import Payload

from wactorz import ext
from wactorz.web import api_actors, runtime


def _purge_fake_submodules() -> dict[str, ModuleType]:
    """Drop every ``wactorz.ext.*`` module, returning what was removed.

    The prefix catches the *real* extensions too, not only the fakes a test
    writes — so without putting them back, the next import builds a fresh module
    object with fresh module state. `wactorz.ext.tts` keeps its availability
    flag there, and a test that resolves one identity while the running app
    holds the other sees a different answer than the app does.
    """
    removed: dict[str, ModuleType] = {}
    for name in list(sys.modules):
        if name.startswith("wactorz.ext."):
            removed[name] = sys.modules.pop(name)
    return removed


@pytest.fixture(name="registry_and_ext")
def registry_and_ext_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, Any, None]:
    """A one-actor registry plus a discoverable extension that tags each payload."""
    (tmp_path / "tagger.py").write_text(
        "def setup(app):\n    pass\n\n\n"
        "def actor_decorator(app):\n"
        "    def decorate(actor, payload):\n"
        "        payload['did'] = f'did:test:{actor.actor_id}'\n"
        "    return decorate\n",
        encoding="utf-8",
    )
    real_extensions = _purge_fake_submodules()
    monkeypatch.setattr(ext, "__path__", [str(tmp_path)])

    actor = SimpleNamespace(actor_id="main", name="main", protected=True, metrics=None)
    monkeypatch.setattr(runtime, "registry", SimpleNamespace(all_actors=lambda: [actor]))
    monkeypatch.setattr(runtime, "state", {"agents": {}})
    yield
    _purge_fake_submodules()
    sys.modules.update(real_extensions)


async def test_actors_handler_applies_registered_decorator(
    registry_and_ext: pytest.FixtureDef,
) -> None:
    req = SimpleNamespace(app=SimpleNamespace())
    resp = await api_actors.actors_handler(req)  # type: ignore[arg-type]
    assert resp.body
    rows = json.loads(resp.body if not isinstance(resp.body, Payload) else resp.body.decode())
    assert rows[0]["id"] == "main"
    assert rows[0]["did"] == "did:test:main"  # injected by the extension, not core


async def test_actors_handler_is_a_noop_without_decorators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty ext dir → no decorators → base payload only, no extra keys.
    _purge_fake_submodules()
    monkeypatch.setattr(ext, "__path__", [str(tmp_path)])
    actor = SimpleNamespace(actor_id="main", name="main", protected=False, metrics=None)
    monkeypatch.setattr(runtime, "registry", SimpleNamespace(all_actors=lambda: [actor]))
    monkeypatch.setattr(runtime, "state", {"agents": {}})
    resp = await api_actors.actors_handler(SimpleNamespace(app=SimpleNamespace()))  # type: ignore[arg-type]
    assert resp.body
    rows = json.loads(resp.body if not isinstance(resp.body, Payload) else resp.body.decode())
    assert "did" not in rows[0]
    _purge_fake_submodules()
