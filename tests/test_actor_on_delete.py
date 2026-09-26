"""`on_delete`: what an agent removes when it is deleted rather than stopped.

A stop is expected to be undone, so it keeps state. A delete promises no trace
is left, and the store purge that follows only knows the stores it knows. The
hook is for the rest: files an agent keeps of its own, retained messages outside
`agents/<id>/`. It runs before the stop, and a failure in it never stops the
deletion itself.
"""

from pathlib import Path

from wactorz.core.actor import Actor, Message


class _Recorder(Actor):
    """An actor that records its hooks, and can be told to fail in `on_delete`."""

    def __init__(self, *, fail: bool = False, **kwargs: object) -> None:
        super().__init__(**kwargs)  # pyright: ignore[reportArgumentType]
        self.calls: list[str] = []
        self.fail = fail

    async def on_delete(self) -> None:
        self.calls.append("on_delete")
        if self.fail:
            raise RuntimeError("could not tidy up")

    async def on_stop(self) -> None:
        self.calls.append("on_stop")

    async def handle_message(self, msg: Message) -> None:
        """Nothing to handle; only the lifecycle is under test."""


async def test_a_delete_runs_the_hook_before_stopping(tmp_path: Path) -> None:
    actor = _Recorder(name="recorder", persistence_dir=str(tmp_path))

    assert await actor.apply_command("delete")

    assert actor.calls == ["on_delete", "on_stop"]


async def test_a_failing_hook_does_not_stop_the_deletion(tmp_path: Path) -> None:
    actor = _Recorder(fail=True, name="recorder", persistence_dir=str(tmp_path))

    assert await actor.apply_command("delete")

    assert actor.calls == ["on_delete", "on_stop"]


async def test_a_stop_leaves_the_hook_alone(tmp_path: Path) -> None:
    # A stop is meant to be undone, so nothing is deleted on its account.
    actor = _Recorder(name="recorder", persistence_dir=str(tmp_path))

    await actor.apply_command("stop")

    assert actor.calls == ["on_stop"]


async def test_by_default_there_is_nothing_to_remove(tmp_path: Path) -> None:
    class _Plain(Actor):
        async def handle_message(self, msg: Message) -> None:
            """Nothing to handle."""

    await _Plain(name="plain", persistence_dir=str(tmp_path)).on_delete()
