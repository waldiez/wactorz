"""The chat attachment endpoints.

`POST /api/upload` takes one multipart file and answers with the id the browser
puts on a chat turn; `GET /api/upload/{id}` serves it back for the thread's
preview. Both are registered only when uploads are turned on, so a deployment
that has not asked for the feature has no endpoint that writes to disk.

The body is read in chunks rather than through `request.post()`: a 25 MB limit
buffered in memory is 25 MB per concurrent upload, and the whole point of the
limit is to stop a caller choosing how much of the process they occupy. It also
means the cap can be enforced while reading, so an oversized body is refused
after one chunk instead of after all of it has arrived.

Every touch of the disk runs on a thread: a write inside the handler would hold
the event loop for as long as the disk takes, and with it every actor in the
process and the broker's keepalive.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import IO, Any

from aiohttp import BodyPartReader, web

from .. import config
from . import uploads

logger = logging.getLogger(__name__)

#: Bytes read from the body at a time: each is one write on a thread, so a full
#: upload costs a hundred or so hops rather than one per small network read.
READ_BYTES = 256 * 1024


def _meta_path(directory: Path, file_id: str) -> Path:
    return directory / f"{file_id}.json"


def _write(handle: IO[bytes], chunk: bytes) -> None:
    """Write one chunk. Run on a thread."""
    handle.write(chunk)


def _keep(staging: Path, target: Path, meta_path: Path, meta: dict[str, Any]) -> None:
    """Put a finished upload where its id resolves, with what is known of it. Run on a thread."""
    staging.replace(target)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _read_meta(meta_path: Path) -> dict[str, Any]:
    """What was recorded of a stored file, or nothing. Run on a thread."""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:  # served as an opaque download, which is the safe default anyway
        return {}
    return meta if isinstance(meta, dict) else {}


async def _receive(part: BodyPartReader, staging: Path) -> tuple[int, bytes, bool]:
    """Write ``part`` to ``staging``: its size, its first bytes, and whether it ran over the limit."""
    size = 0
    head = b""
    handle = await asyncio.to_thread(staging.open, "wb")
    try:
        while chunk := await part.read_chunk(READ_BYTES):
            size += len(chunk)
            if size > config.UPLOAD_MAX_BYTES:
                # A flag, not a raise: raised, it would share an except with every
                # ValueError from the read or the write, and each of those would be
                # reported as too large.
                return size, head, True
            if len(head) < uploads.SNIFF_BYTES:
                head += chunk[: uploads.SNIFF_BYTES - len(head)]
            await asyncio.to_thread(_write, handle, chunk)
    finally:
        await asyncio.to_thread(handle.close)
    return size, head, False


async def upload_handler(request: web.Request) -> web.Response:
    """Store one uploaded file and answer with what the browser needs to show it."""
    try:
        reader = await request.multipart()
        part = await reader.next()
    except Exception:
        return web.json_response({"error": "malformed upload"}, status=400)

    # A nested multipart reads back as a MultipartReader rather than a file part,
    # so this is a correctness check as much as a narrowing one.
    if not isinstance(part, BodyPartReader) or part.name != "file":
        return web.json_response({"error": "expected a 'file' part"}, status=400)

    directory = await asyncio.to_thread(uploads.upload_dir)
    file_id = uploads.new_id()
    target = directory / file_id
    # Written under a temporary name so a refused or interrupted upload never
    # leaves a file the id would resolve to.
    staging = directory / f".{file_id}.part"

    try:
        size, head, too_large = await _receive(part, staging)
    except Exception as exc:
        await asyncio.to_thread(staging.unlink, missing_ok=True)
        logger.warning("[upload] failed: %s", exc)
        return web.json_response({"error": "upload failed"}, status=400)

    if too_large:
        await asyncio.to_thread(staging.unlink, missing_ok=True)
        return web.json_response(
            {"error": f"larger than {config.UPLOAD_MAX_BYTES} bytes"}, status=413
        )

    if not size:
        await asyncio.to_thread(staging.unlink, missing_ok=True)
        return web.json_response({"error": "empty file"}, status=400)

    name = uploads.safe_name(part.filename or "")
    mime = uploads.sniff(head)
    meta = {"name": name, "mime": mime, "size": size}
    await asyncio.to_thread(_keep, staging, target, _meta_path(directory, file_id), meta)
    logger.info("[upload] stored %s (%s, %d bytes)", file_id, mime, size)
    return web.json_response({"id": file_id, **meta})


async def download_handler(request: web.Request) -> web.StreamResponse:
    """Serve a stored attachment back to the thread that references it."""
    file_id = request.match_info.get("file_id", "")
    if not uploads.is_id(file_id):
        # Rejected on shape, so nothing caller-supplied ever reaches a path.
        return web.json_response({"error": "not found"}, status=404)

    directory = await asyncio.to_thread(uploads.upload_dir)
    target = directory / file_id
    if not await asyncio.to_thread(target.is_file):
        return web.json_response({"error": "not found"}, status=404)

    meta = await asyncio.to_thread(_read_meta, _meta_path(directory, file_id))

    content_type, inline = uploads.serve_type(str(meta.get("mime", "")))
    name = uploads.safe_name(str(meta.get("name", "")) or file_id)
    headers = {
        # Set explicitly: stored files carry no extension, so the type would
        # otherwise be guessed from a name that does not exist.
        "Content-Type": content_type,
        # Sniffing is what would turn a download back into something the browser
        # decides to render, so the header choice below is not the only rule.
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": (
            f'inline; filename="{name}"' if inline else f'attachment; filename="{name}"'
        ),
    }
    return web.FileResponse(target, headers=headers, chunk_size=64 * 1024)
