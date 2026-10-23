"""Building the deck must not stop the rest of the process while it runs.

Writing a deck reads every picture and writes a file of several megabytes, all
of it blocking. Called inline from ``handle_task`` nothing else in the process
would run for that long — not other agents, not the broker read, and not the
framework's own task timeout, which could then never fire on the very work it
exists to bound.
"""

import asyncio
import json
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.programs import program_namespace
from wactorz.documents import DOCUMENT_DIRS_ENV

# The agent ships as source that the framework exec's when spawning it, so the
# only way to reach its helpers is the way the framework does.
_agent = program_namespace("doc_to_pptx_agent.py")

_OUTLINE = {"title": "T", "slides": [{"index": 0, "type": "title", "title": "T"}]}


class _Agent:
    def __init__(self) -> None:
        self.llm = self

    async def chat(self, _prompt: str) -> str:
        return json.dumps(_OUTLINE)

    async def log(self, _text: str) -> None:
        pass

    async def alert(self, _message: str, _severity: str = "warning") -> None:
        pass


@pytest.fixture(name="request_")
def request_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setenv(DOCUMENT_DIRS_ENV, str(docs))
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    source = docs / "notes.txt"
    source.write_text("Revenue grew.", encoding="utf-8")
    return {"file_path": str(source), "nim_fallback": False}


@pytest.fixture(name="held")
def held_fixture(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """A build that blocks its caller until the test lets it go."""
    release = threading.Event()

    def build(_outline: Any, _pictures: Any, output_path: str) -> None:
        if not release.wait(timeout=30):
            raise TimeoutError("the test never released the build")
        Path(output_path).write_bytes(b"deck")

    monkeypatch.setitem(_agent, "_build_deck", build)
    return release


class TestTheBuild:
    async def test_the_event_loop_keeps_running(
        self, request_: dict[str, Any], held: threading.Event
    ) -> None:
        turns = asyncio.Event()

        async def _other_work() -> None:
            await asyncio.sleep(0)
            turns.set()
            # The build waits for this turn: inline, it would never come.
            held.set()

        other = asyncio.create_task(_other_work())
        result = await _agent["handle_task"](_Agent(), request_)
        await other

        assert turns.is_set()
        assert result["error"] is None
