"""A stand-in for `wactorz.client.Client`, for tests of the panes and the app.

What the context does with a real server is in `test_context.py`, against the real
application. These only need something to draw.
"""

from collections.abc import AsyncIterator
from typing import Any, cast

from wactorz.client import Attachment, Client, Cost, Part, Server
from wactorz.tui.context import TUIContext


class _Chat:
    def __init__(self, parts: tuple[Part, ...], rows: list[dict[str, Any]]) -> None:
        self.parts = parts
        self.rows = rows
        self.asked: list[str] = []
        self.addressed: list[str | None] = []
        self.attached: list[list[Attachment]] = []
        self.fails_with: Exception | None = None

    async def send(
        self, text: str, *, to: str | None = None, attachments: Any = ()
    ) -> AsyncIterator[Part]:
        self.asked.append(text)
        self.addressed.append(to)
        self.attached.append(list(attachments))
        for found in self.parts:
            yield found
        if self.fails_with is not None:
            raise self.fails_with

    async def history(self, *, limit: int = 200, **_: Any) -> list[dict[str, Any]]:
        return self.rows[:limit]

    async def commands(self) -> list[dict[str, Any]]:
        return [{"typed": "/agents"}, {"typed": "/plans approve"}]

    async def stop_all(self) -> int:
        return 1


class _Files:
    async def upload(self, source: Any) -> Attachment:
        return Attachment("0" * 32, name=source.name, size=source.stat().st_size)


class _System:
    async def cost(self) -> Cost:
        return Cost(spend_usd=0.0, limit_usd=None, period="monthly")

    async def logs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return []


class FakeClient:
    """Answers with nothing much, and never connects."""

    url = "http://server.test:8888"

    def __init__(
        self,
        *,
        parts: tuple[Part, ...] = (),
        rows: list[dict[str, Any]] | None = None,
        uploads: bool = True,
    ) -> None:
        self.chat = _Chat(parts, rows or [])
        self.files = _Files()
        self.system = _System()
        self.uploads = uploads

    async def server(self) -> Server:
        return Server(
            version="1.2.3",
            uploads=self.uploads,
            upload_max_bytes=1024,
            uploads_per_message=2,
            llm_provider="fake",
            llm_model="scripted",
        )

    async def events(self) -> AsyncIterator[Any]:
        return
        yield


def part(text: str, agent: str = "main") -> Part:
    """What one reply says."""
    return Part(text=text, agent=agent, agent_id="", streaming=True)


def context_over(client: FakeClient | None = None) -> TUIContext:
    """A context with its server details read, as `TUIContext.start` leaves it."""
    context = TUIContext(cast(Client, client or FakeClient()))
    context.server = Server(
        version="1.2.3",
        uploads=client.uploads if client else True,
        upload_max_bytes=1024,
        uploads_per_message=2,
        llm_provider="fake",
        llm_model="scripted",
    )
    return context


__all__ = ["FakeClient", "context_over", "part"]
