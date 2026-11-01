"""Files stored on the server, to send with a message or read back."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp

from .transport import Transport

#: Seconds a transfer may take: a large file over a slow link is healthy.
TRANSFER_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class Attachment:
    """A file stored on the server, to send with a message by its ``id``."""

    id: str
    name: str = ""
    mime: str = ""
    size: int = 0


class Files:
    """The server's store of uploaded files."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    async def upload(self, source: str | os.PathLike[str] | Attachment) -> Attachment:
        """Store a file on the server. An `Attachment` already stored is returned as it is."""
        if isinstance(source, Attachment):
            return source
        path = Path(source)
        content = await asyncio.to_thread(path.read_bytes)
        form = aiohttp.FormData()
        form.add_field("file", content, filename=path.name)
        stored = await self._transport.request(
            "POST", "/api/upload", data=form, timeout=TRANSFER_TIMEOUT_S
        )
        return Attachment(
            id=str(stored["id"]),
            name=str(stored.get("name") or ""),
            mime=str(stored.get("mime") or ""),
            size=int(stored.get("size") or 0),
        )

    async def download(self, attachment: Attachment | str) -> bytes:
        """The bytes of a stored file."""
        file_id = attachment.id if isinstance(attachment, Attachment) else attachment
        return await self._transport.request_bytes(
            f"/api/upload/{quote(file_id, safe='')}", timeout=TRANSFER_TIMEOUT_S
        )
