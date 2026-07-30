"""State-directory resolution — one path, resolved one way.

The regression these pin: agent factories and the central stores used to
resolve the state directory independently, so a configured
``WACTORZ_STATE_DIR`` moved some writes and left others in a cwd-relative
``./state``.
"""

import importlib
import os
from pathlib import Path

import pytest

import wactorz.reset as reset_mod
from wactorz.core.paths import ensure_state_dir, resolve_state_dir
from wactorz.core.persistence import init_persistence
from wactorz.core.registry import ActorSystem


class TestResolveStateDir:
    def test_explicit_wins_over_env(self, monkeypatch) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", "/from/env")
        assert resolve_state_dir("/explicit") == "/explicit"

    def test_env_used_when_no_argument(self, monkeypatch) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", "/from/env")
        assert resolve_state_dir() == "/from/env"

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("WACTORZ_STATE_DIR", raising=False)
        assert resolve_state_dir() == "./state"

    def test_blank_env_counts_as_unset(self, monkeypatch) -> None:
        # A bare `WACTORZ_STATE_DIR=` in a .env file must not resolve to ""
        # and drop every store into the process's working directory.
        monkeypatch.setenv("WACTORZ_STATE_DIR", "   ")
        assert resolve_state_dir() == "./state"

    def test_resolve_does_not_create(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "not-yet"
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(target))
        assert resolve_state_dir() == str(target)
        assert not target.exists()

    def test_ensure_creates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "made" / "deep"
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(target))
        assert ensure_state_dir() == str(target)
        assert target.is_dir()


class TestStoresHonourTheEnv:
    def test_init_persistence_puts_db_and_pickles_together(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = tmp_path / "st"
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(state))
        monkeypatch.chdir(tmp_path)

        init_persistence(run_migration=False)
        assert (state / "wactorz.db").exists()
        # The regression: nothing may land in a cwd-relative ./state.
        assert not (tmp_path / "state").exists()

    def test_actor_system_reads_the_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = tmp_path / "st"
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(state))
        assert ActorSystem()._state_dir == str(state)

    def test_actor_system_argument_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path / "env"))
        explicit = str(tmp_path / "explicit")
        assert ActorSystem(state_dir=explicit)._state_dir == explicit


class TestResetTargetsTheSameDir:
    def test_reset_module_default_matches_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # reset.py resolves at import time; re-import under a set env and the
        # wipe must target the same place the app writes to.
        state = str(tmp_path / "st")
        monkeypatch.setenv("WACTORZ_STATE_DIR", state)

        # reload mutates the module object in place, so reset_mod is the fresh one
        importlib.reload(reset_mod)
        try:
            assert state == reset_mod._DEFAULT_STATE
            assert os.path.join(state, "wactorz.db") == reset_mod._DEFAULT_DB
        finally:
            monkeypatch.delenv("WACTORZ_STATE_DIR", raising=False)
            importlib.reload(reset_mod)

    def test_importing_reset_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("WACTORZ_STATE_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        importlib.reload(reset_mod)
        assert not (tmp_path / "state").exists()


class TestBootstrapAgreesWithResolver:
    """_bootstrap resolves the log directory inline to stay importable before the
    rest of the package. These pin the two implementations together so the
    duplication cannot drift."""

    def _bootstrap_resolution(self) -> str:
        # Mirror of the expression in wactorz/_bootstrap.py.
        return os.environ.get("WACTORZ_STATE_DIR", "").strip() or "./state"

    def test_agrees_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WACTORZ_STATE_DIR", raising=False)
        assert self._bootstrap_resolution() == resolve_state_dir()

    def test_agrees_when_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", str(tmp_path / "st"))
        assert self._bootstrap_resolution() == resolve_state_dir()

    def test_agrees_when_blank(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WACTORZ_STATE_DIR", "   ")
        assert self._bootstrap_resolution() == resolve_state_dir()

    def test_bootstrap_source_matches_this_mirror(self) -> None:
        # If the expression in _bootstrap changes, this mirror must change too.
        src = (Path(__file__).parent.parent / "wactorz" / "_bootstrap.py").read_text()
        assert 'os.environ.get("WACTORZ_STATE_DIR", "").strip() or "./state"' in src
