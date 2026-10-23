"""Main knows which models are kept, and says so to the user and to its model.

`/artifacts` lists them in chat. Main's system prompt lists them too, read off
the event loop before each turn, so a spawn it writes names a model that is
there rather than one it imagined; with none kept, the prompt says nothing of
them beyond how to use one.
"""

from pathlib import Path
from typing import Any, cast

import pytest

from wactorz.agents.main import memory as memory_module
from wactorz.agents.main.actor import MainActor
from wactorz.agents.main.commands import info, registry
from wactorz.agents.main.commands.dispatch import CommandContext
from wactorz.core.artifacts import ArtifactStore, summary


@pytest.fixture(name="state")
def state_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Main's state directory, where the command and the prompt look for models."""
    state = tmp_path / "state"
    monkeypatch.setattr(info, "resolve_state_dir", lambda: str(state))
    monkeypatch.setattr(memory_module, "resolve_state_dir", lambda: str(state))
    return state


@pytest.fixture(name="store")
def store_fixture(state: Path) -> ArtifactStore:
    return ArtifactStore.in_state_dir(state)


def add(store: ArtifactStore, tmp_path: Path, name: str, data: bytes, **metadata: Any) -> None:
    path = tmp_path / f"{name}-{len(data)}.onnx"
    path.write_bytes(data)
    store.add(path, name, metadata={"source": "cli", **metadata})


# ── The summary both read ─────────────────────────────────────────────────────


class TestTheSummary:
    def test_each_name_says_its_newest_version(self, store: ArtifactStore, tmp_path: Path) -> None:
        add(store, tmp_path, "imu-detector", b"a" * 10, dataset="imu-2026")
        add(store, tmp_path, "imu-detector", b"b" * 2048, auc=0.93)
        add(store, tmp_path, "yolo", b"c")

        assert summary(store) == [
            "imu-detector — newest imu-detector:2 (onnx, 2.0 KB), 2 versions; auc=0.93",
            "yolo — newest yolo:1 (onnx, 1 B)",
        ]

    def test_one_that_runs_code_says_so(self, store: ArtifactStore, tmp_path: Path) -> None:
        path = tmp_path / "m.pkl"
        path.write_bytes(b"x")
        store.add(path, "legacy", allow_code=True)

        (line,) = summary(store)

        assert "runs code when it loads" in line

    def test_where_it_came_from_is_left_out(self, store: ArtifactStore, tmp_path: Path) -> None:
        add(store, tmp_path, "m", b"x")

        assert "source" not in summary(store)[0]

    def test_a_long_list_is_cut(self, store: ArtifactStore, tmp_path: Path) -> None:
        for index in range(5):
            add(store, tmp_path, f"m{index}", bytes([index]))

        assert len(summary(store, limit=3)) == 3


# ── /artifacts ────────────────────────────────────────────────────────────────


def ctx() -> CommandContext:
    # The command reads nothing of the actor; it stands in for one.
    return CommandContext(actor=cast(MainActor, object()))


class TestTheCommand:
    async def test_it_lists_what_is_kept(self, store: ArtifactStore, tmp_path: Path) -> None:
        add(store, tmp_path, "imu-detector", b"x")

        reply = await info.show_artifacts(ctx(), "")

        assert reply.startswith("Models kept on main:")
        assert "imu-detector — newest imu-detector:1" in reply

    async def test_with_none_kept_it_says_how_to_add_one(self, store: ArtifactStore) -> None:
        reply = await info.show_artifacts(ctx(), "")

        assert "wactorz-artifacts add" in reply

    async def test_a_name_lists_its_versions(self, store: ArtifactStore, tmp_path: Path) -> None:
        add(store, tmp_path, "imu-detector", b"x")
        add(store, tmp_path, "imu-detector", b"yy")

        reply = await info.show_artifacts(ctx(), "imu-detector")

        assert "imu-detector:1" in reply and "imu-detector:2" in reply

    async def test_an_unknown_name_points_back_at_the_list(self, store: ArtifactStore) -> None:
        reply = await info.show_artifacts(ctx(), "nothing")

        assert "No model named 'nothing'" in reply

    async def test_a_store_it_cannot_read_is_said(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        add(store, tmp_path, "m", b"x")
        store.index_path.write_text("{not json", encoding="utf-8")

        reply = await info.show_artifacts(ctx(), "")

        assert reply.startswith("Cannot list models:")

    @pytest.mark.parametrize(("typed", "argument"), [("/artifacts", ""), ("/artifacts imu", "imu")])
    def test_it_is_what_is_typed(self, typed: str, argument: str) -> None:
        found = registry.find(typed)

        assert found is not None
        command, given = found
        assert command.handler is info.show_artifacts
        assert given == argument

    def test_a_longer_word_is_not_it(self) -> None:
        found = registry.find("/artifactsx")

        assert found is None or found[0].handler is not info.show_artifacts

    def test_it_is_in_the_help(self) -> None:
        assert any(line.strip().startswith("/artifacts ") for line in info.HELP_LINES)


# ── Main's prompt ─────────────────────────────────────────────────────────────


@pytest.fixture(name="main")
def main_fixture(tmp_path: Path, state: Path) -> MainActor:
    return MainActor(llm_provider=None, persistence_dir=str(tmp_path / "main"))


class TestMainsPrompt:
    async def test_it_names_the_models_that_are_kept(
        self, main: MainActor, store: ArtifactStore, tmp_path: Path
    ) -> None:
        add(store, tmp_path, "imu-detector", b"x", dataset="imu-2026")

        await main._refresh_stored_models()
        main._rebuild_system_prompt()

        assert "== STORED MODELS" in main.system_prompt
        assert (
            "imu-detector — newest imu-detector:1 (onnx, 1 B); dataset=imu-2026"
            in main.system_prompt
        )

    async def test_with_none_kept_it_lists_none(
        self, main: MainActor, store: ArtifactStore
    ) -> None:
        await main._refresh_stored_models()
        main._rebuild_system_prompt()

        assert "== STORED MODELS" not in main.system_prompt
        assert "== MODELS (ARTIFACTS) ==" in main.system_prompt, "how to use one is always said"

    async def test_one_added_since_is_there_on_the_next_turn(
        self, main: MainActor, store: ArtifactStore, tmp_path: Path
    ) -> None:
        await main._refresh_stored_models()
        add(store, tmp_path, "yolo", b"x")

        await main._refresh_stored_models()
        main._rebuild_system_prompt()

        assert "yolo — newest yolo:1" in main.system_prompt

    async def test_a_store_it_cannot_read_leaves_the_block_out(
        self, main: MainActor, store: ArtifactStore, tmp_path: Path
    ) -> None:
        add(store, tmp_path, "m", b"x")
        store.index_path.write_text("{not json", encoding="utf-8")

        await main._refresh_stored_models()
        main._rebuild_system_prompt()

        assert "== STORED MODELS" not in main.system_prompt

    def test_it_no_longer_says_arrays_are_dropped_in_a_move(self, main: MainActor) -> None:
        main._rebuild_system_prompt()

        assert "Non-serialisable objects (numpy arrays" not in main.system_prompt
        assert "numpy arrays\nand torch tensors" in main.system_prompt


# ── A terminal command typed into chat ────────────────────────────────────────

TYPED = r"wactorz-artifacts add C:\Users\me\examples\im.npz yolo-model"


class TestATerminalCommandInChat:
    """Main cannot run Wactorz's programs, so it never lets a model say it did."""

    @pytest.mark.parametrize(
        ("text", "program"),
        [
            (TYPED, "wactorz-artifacts"),
            ("wactorz-artifacts", "wactorz-artifacts"),
            ("  wactorz-state export  ", "wactorz-state"),
            ("wactorz-reset --all", "wactorz-reset"),
            ("wactorz-node --node pi", "wactorz-node"),
            ("wactorz-artifactsx add a b", None),
            ("please run wactorz-artifacts list", None),
            ("", None),
        ],
    )
    def test_it_is_recognised_by_its_first_word(self, text: str, program: str | None) -> None:
        assert info.terminal_command(text) == program

    async def test_the_reply_says_nothing_was_run_and_how_to_run_it(
        self, store: ArtifactStore, tmp_path: Path
    ) -> None:
        add(store, tmp_path, "imu-detector", b"x")

        reply = await info.terminal_command_reply(TYPED)

        assert reply is not None
        assert "nothing was run" in reply
        assert TYPED in reply
        assert "imu-detector — newest imu-detector:1" in reply, "what is kept, so it is plain"
        assert "yolo-model" not in reply.split("What is kept now:")[1]

    async def test_another_program_is_not_followed_by_the_models(
        self, store: ArtifactStore
    ) -> None:
        reply = await info.terminal_command_reply("wactorz-state export")

        assert reply is not None and "What is kept now" not in reply

    async def test_anything_else_is_left_alone(self) -> None:
        assert await info.terminal_command_reply("add the yolo model please") is None

    async def test_main_answers_it_without_its_model(
        self, main: MainActor, store: ArtifactStore
    ) -> None:
        # No model is configured, so an answer that came from one could not be this.
        reply = await main._process_user_input(TYPED)

        assert "nothing was run" in reply
        assert store.names() == []

    async def test_main_answers_it_when_streaming_too(
        self, main: MainActor, store: ArtifactStore
    ) -> None:
        chunks = [chunk async for chunk in main.process_user_input_stream(TYPED)]

        assert any(isinstance(chunk, str) and "nothing was run" in chunk for chunk in chunks)

    async def test_and_on_a_channel_without_admin_commands(
        self, main: MainActor, store: ArtifactStore
    ) -> None:
        reply = await main._process_user_input_restricted(TYPED)

        assert "nothing was run" in reply

    def test_the_prompt_says_main_cannot_store_one(self, main: MainActor) -> None:
        main._rebuild_system_prompt()

        assert "Never say that a model was added" in main.system_prompt
