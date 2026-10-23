"""An actor's and a store's state directories are made when written to, not when built.

Building an object is not the moment to touch the disk: a test, or a script that
only inspects one, would otherwise leave directories wherever it looked.
"""

from pathlib import Path

import pytest

from wactorz.core.actor import Actor
from wactorz.core.persistence.pickle_store import PickleStore


class _Quiet(Actor):
    """An actor that does nothing, so starting one does not reach the broker."""

    async def handle_message(self, msg: object) -> None:
        return None


@pytest.fixture(name="quiet")
def quiet_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Quiet:
    actor = _Quiet(name="quiet", persistence_dir=str(tmp_path / "state"))

    async def _nothing(*_args: object, **_kwargs: object) -> None:
        return None

    # Starting it starts loops that talk to the broker; only the directory is under test.
    monkeypatch.setattr(actor, "on_start", _nothing)
    monkeypatch.setattr(actor, "_message_loop", _nothing)
    monkeypatch.setattr(actor, "_heartbeat_loop", _nothing)
    monkeypatch.setattr(actor, "_command_listener", _nothing)
    monkeypatch.setattr(actor, "_publish_status", _nothing)
    return actor


class TestAnActor:
    def test_building_one_makes_nothing(self, quiet: _Quiet, tmp_path: Path) -> None:
        assert not (tmp_path / "state").exists()

    async def test_starting_it_makes_its_directory(self, quiet: _Quiet, tmp_path: Path) -> None:
        await quiet.start()
        try:
            assert (tmp_path / "state" / "quiet").is_dir()
        finally:
            await quiet.stop()

    def test_its_state_dir_exists_whenever_it_is_asked_for(
        self, quiet: _Quiet, tmp_path: Path
    ) -> None:
        # A public promise to agents that keep files of their own there.
        assert quiet.state_dir.is_dir()
        assert quiet.state_dir == tmp_path / "state" / "quiet"


class TestAPickleStore:
    def test_building_and_reading_make_nothing(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path / "state"))

        assert store.load("someone") == {}
        assert not (tmp_path / "state").exists()

    def test_the_first_write_makes_the_directory(self, tmp_path: Path) -> None:
        store = PickleStore(str(tmp_path / "state"))

        store.save("someone", {"count": 1})
        store.flush()

        assert (tmp_path / "state" / "someone" / "state.pkl").is_file()
        assert PickleStore(str(tmp_path / "state")).load("someone") == {"count": 1}
