"""`Client`: one running server, from outside it."""

import os
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from .chat import Chat
from .errors import NoServerError
from .files import Files
from .frames import Event
from .questions import Questions
from .stream import Listener, Stream
from .system import System
from .transport import DEFAULT_TIMEOUT_S, Transport

#: Where `Client.from_env` finds the server and its key. Not the server's own
#: ``API_KEY``, so a machine running a server does not offer its key to another.
URL_VARIABLE = "WACTORZ_URL"
KEY_VARIABLE = "WACTORZ_API_KEY"


@dataclass(frozen=True)
class Server:
    """What a server says about itself."""

    version: str
    uploads: bool
    #: The most a stored file may be, and how many may go with one message.
    upload_max_bytes: int = 0
    uploads_per_message: int = 0
    llm_provider: str = ""
    llm_model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class Client:
    """A running Wactorz, reached over its REST API and socket.

    Holds the connection and nothing else: what a server does is reached by its
    noun, ``wz.chat``, ``wz.files``, ``wz.questions``, one area each, so a new area
    adds no method here. Use it as an async context manager. The key is left out of ``repr``, and is
    taken as a value here or from the environment, never from a command line.
    """

    def __init__(self, url: str, *, api_key: str = "", timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._transport = Transport(url, api_key, timeout=timeout)
        self._stream = Stream(self._transport)
        self.files = Files(self._transport)
        self.chat = Chat(self._transport, self._stream, self.files)
        self.questions = Questions(self._transport)
        self.system = System(self._transport)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Client":
        """The server named by ``WACTORZ_URL``, with ``WACTORZ_API_KEY``."""
        env = os.environ if environ is None else environ
        url = env.get(URL_VARIABLE, "").strip()
        if not url:
            raise NoServerError(URL_VARIABLE)
        return cls(url, api_key=env.get(KEY_VARIABLE, ""))

    def __repr__(self) -> str:
        return f"Client({self.url!r})"

    @property
    def url(self) -> str:
        """The server this client talks to."""
        return self._transport.url

    async def __aenter__(self) -> "Client":
        await self.open()
        return self

    async def __aexit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def open(self) -> None:
        """Start the session. The socket waits until something needs it."""
        await self._transport.open()

    async def close(self) -> None:
        """Close the socket and the session."""
        await self._stream.close()
        await self._transport.close()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Call any route and return its JSON: anything without a helper here."""
        return await self._transport.request(method, path, **kwargs)

    async def server(self) -> Server:
        """The server's version and what it offers."""
        config = await self.request("GET", "/api/config")
        uploads, llm = config.get("uploads") or {}, config.get("llm") or {}
        return Server(
            version=str(config.get("version") or ""),
            uploads=bool(uploads.get("enabled")),
            upload_max_bytes=int(uploads.get("max_bytes") or 0),
            uploads_per_message=int(uploads.get("max_per_message") or 0),
            llm_provider=str(llm.get("provider") or ""),
            llm_model=str(llm.get("model") or ""),
            raw=config,
        )

    async def events(self) -> AsyncGenerator[Event, None]:
        """Everything the socket hears, starting with the current state."""
        listener = self._stream.listen(Listener())
        try:
            while True:
                event = await listener.get()
                if event is None:
                    return
                yield event
        finally:
            self._stream.forget(listener)
