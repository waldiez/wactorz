"""Turning stored attachments into something a model can read.

A chat turn carries attachment ids; a model request carries content blocks. This
is the conversion, and the size rule that governs it.

⚠ **A file can be small enough to upload and still too large to send.** Base64
inflates by about a third, and the request as a whole is capped — so the usable
inline size is well under the upload limit. A file over it is named to the model
rather than dropped, so the agent can say it could not read the file instead of
answering as though nothing was attached.

Audio is named and not transcribed. Speech-to-text is separate work; when it
lands it fills in the content without changing this shape.
"""

import base64
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Largest request a model will accept. Everything in the turn shares it.
_REQUEST_LIMIT_BYTES = 32 * 1024 * 1024

#: Base64 costs four bytes per three, and the surrounding JSON is not free, so
#: the raw file has to be comfortably under the request limit rather than near it.
MAX_INLINE_BYTES = int(_REQUEST_LIMIT_BYTES / 1.4)

#: How much of an attachment's text survives into conversation history. The
#: stand-in exists so a later turn knows a file was attached, not so it can be
#: re-read from history — the full text goes to the model in the turn it arrives.
HISTORY_TEXT_LIMIT = 2000

#: Types a model reads as an image.
_IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

#: Types a model reads as a document.
_DOCUMENT_TYPES = frozenset({"application/pdf"})


def _note(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _describe(name: str, reason: str) -> dict[str, str]:
    """Tell the model a file is there and why it cannot read it.

    Silence would be worse than useless: the user can see they attached
    something, so an answer that ignores it reads as the agent ignoring them.
    """
    return _note(f"[attachment: {name} — {reason}]")


def to_blocks(
    attachments: list[dict[str, Any]],
    read_bytes: Callable[[str], bytes | None],
) -> list[dict[str, Any]]:
    """Content blocks for `attachments`, in the order they were attached.

    Blocks come before the user's text, which is what the models expect: the
    question reads as being about the files rather than the other way round.
    """
    blocks: list[dict[str, Any]] = []
    for item in attachments:
        name = str(item.get("name") or "attachment")
        mime = str(item.get("mime") or "")
        size = int(item.get("size") or 0)

        if size > MAX_INLINE_BYTES:
            blocks.append(_describe(name, "too large to send to the model"))
            continue

        raw = read_bytes(str(item.get("id") or ""))
        if raw is None:
            # The reference outlived the file. Saying so beats a silent gap.
            blocks.append(_describe(name, "no longer available"))
            continue

        if mime in _IMAGE_TYPES or mime in _DOCUMENT_TYPES:
            # Named before the data block rather than left anonymous: the model
            # cannot otherwise answer "what does screenshot.png show", and the
            # name is what makes this block flattenable for the providers and
            # the history stand-in that take text only.
            blocks.append(_note(f"[attachment: {name}]"))
            blocks.append(
                {
                    "type": "image" if mime in _IMAGE_TYPES else "document",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        # No newlines: the encoded string is sent verbatim.
                        "data": base64.b64encode(raw).decode("ascii"),
                    },
                }
            )
        elif _is_text(raw):
            blocks.append(_note(f"[attachment: {name}]\n{raw.decode('utf-8', 'replace')}"))
        elif mime.startswith("audio/"):
            blocks.append(_describe(name, "audio, which cannot be transcribed yet"))
        else:
            blocks.append(_describe(name, "a file type that cannot be read"))
    return blocks


def flatten(blocks: list[dict[str, Any]], limit: int | None = None) -> str:
    """The text-only reading of `blocks`, for the two places that take no blocks.

    Providers that cannot carry multimodal content get this instead, so a file
    is always named and a text attachment is always readable — a model that
    cannot see the image can still say which one it was sent.

    It is also what goes into conversation history. The blocks themselves are
    spliced into a single request and deliberately not persisted: base64 in the
    history would be written to disk every turn and re-sent on every turn after.
    The stand-in leaves the next turn knowing a file was attached and what it
    was called, which is enough to ask for it again — hence `limit`, which
    bounds a large text attachment there without touching what the model is
    sent now.
    """
    parts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
    text = "\n".join(p for p in parts if p)
    if limit is not None and len(text) > limit:
        return text[:limit] + "\n[…truncated]"
    return text


def _is_text(raw: bytes) -> bool:
    """Whether these bytes are text a model can be shown directly.

    Decided by content rather than by the type the upload was labelled with: a
    `.csv` and a `.json` are both just text, and the label is the one part of an
    upload nobody verified.
    """
    if b"\x00" in raw[:8192]:
        return False
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True
