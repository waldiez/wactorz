"""Storage rules for chat file attachments.

Three properties, each load-bearing:

* **The caller never names a file on disk.** Stored names are generated, so no
  part of the path comes from a request. The state directory holds pickles that
  are loaded and executed, which makes any write a caller can steer into a
  code-execution primitive rather than a disk-space problem.
* **The type comes from the bytes, not the claim.** A browser sends whatever
  `Content-Type` it likes and a filename extension is decoration. Only the
  signatures below produce an accepted type, so anything unrecognised is stored
  as an opaque download and never as something a browser will render.
* **`image/svg+xml` can never be the answer.** An SVG is a script container, and
  it is text, so it has no signature here — it lands in the opaque bucket and is
  served as a download. That is deliberate: the accept-list the browser applies
  matches on the `image/` prefix, which includes SVG.
"""

import json
import re
import uuid
from pathlib import Path
from urllib.parse import unquote

from .. import config
from ..core.paths import resolve_state_dir

#: Leading bytes that identify a type we are willing to serve back inline.
#: Ordered longest-first so a prefix cannot shadow a longer signature.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # docx/xlsx/pptx are zip containers
    (b"\xd0\xcf\x11\xe0", "application/x-ole-storage"),  # legacy doc/xls/ppt
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"ID3", "audio/mpeg"),
)

#: Types safe to hand a browser with the content type we sniffed. Everything
#: else is served as a download, so it cannot execute in the page's origin.
INLINE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

#: Fallback for content no signature matches — a plain download, never rendered.
OPAQUE_TYPE = "application/octet-stream"

#: How much of the stream is needed to decide. The longest signature is 12 bytes
#: (RIFF/WEBP); the rest is slack so this never has to change with the table.
SNIFF_BYTES = 32

#: How many attachments one message may carry. A turn is composed by hand, so
#: this is only a bound on what a crafted message can make the server look up.
MAX_PER_MESSAGE = 20

#: Stored ids are generated here, so a lookup can reject anything else outright
#: rather than relying on path containment to catch a traversal.
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def sniff(head: bytes) -> str:
    """The type these bytes actually are, or the opaque fallback."""
    if head[:4] == b"RIFF" and head[8:12] in (b"WEBP", b"WAVE"):
        return "image/webp" if head[8:12] == b"WEBP" else "audio/wav"
    for signature, mime in _SIGNATURES:
        if head.startswith(signature):
            return mime
    return OPAQUE_TYPE


def serve_type(stored_mime: str) -> tuple[str, bool]:
    """The content type to serve and whether it may render in the page.

    Anything not positively identified as a still image is a download, which is
    what stops an upload becoming script in this origin.
    """
    if stored_mime in INLINE_TYPES:
        return stored_mime, True
    return OPAQUE_TYPE, False


def new_id() -> str:
    return uuid.uuid4().hex


def is_id(value: str) -> bool:
    """Whether `value` is one of ours. Shape only — no filesystem access."""
    return bool(_ID_RE.match(value or ""))


def upload_dir(state_dir: str | None = None) -> Path:
    """Where uploads live, created on demand.

    Under the state directory so a deployment that persists state persists
    attachments with it, in a subdirectory of its own so a stored file can never
    occupy the path an agent's pickle is read from.
    """
    path = Path(resolve_state_dir(state_dir)) / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_name(raw: str) -> str:
    """The original filename, reduced to something safe to echo back.

    Kept for display only — it never reaches the filesystem. Returned to the
    browser and rendered in a chip, so it is stripped of anything that would let
    it read as a path or break out of the text it appears in.
    """
    # Decoded first: a multipart filename arrives percent-encoded, so `%2F` is a
    # separator that survives a check for `/` and becomes one again downstream.
    name = unquote(raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    name = re.sub(r'[<>:"|?*]', "", name)
    return name[:120] or "attachment"


def metadata(file_id: str, state_dir: str | None = None) -> dict[str, object] | None:
    """What was stored under `file_id`, or None if it is not ours.

    Read on the send path rather than trusted from the request: a client knows
    an id, but the name, type and size are the server's own record. Taking them
    from the message would let a caller label a file as anything it liked in
    every thread that shows it.
    """
    if not is_id(file_id):
        return None
    try:
        raw = (upload_dir(state_dir) / f"{file_id}.json").read_text(encoding="utf-8")
        stored = json.loads(raw)
    except Exception:
        return None
    if not isinstance(stored, dict):
        return None
    return {
        "id": file_id,
        "name": safe_name(str(stored.get("name", ""))),
        "mime": str(stored.get("mime", OPAQUE_TYPE)),
        "size": int(stored.get("size", 0) or 0),
    }


def read_bytes(file_id: str, state_dir: str | None = None) -> bytes | None:
    """The stored bytes for `file_id`, or None if it cannot be read.

    Reached through the same three steps the download handler uses — shape
    check, then the generated name under the upload directory — so no part of
    the path comes from a caller and traversal is impossible by construction
    rather than caught afterwards.

    The size is taken from the filesystem before the read, not after: a file
    larger than the endpoint would ever have accepted is refused rather than
    pulled into memory to be measured. That bound and the model's own inline
    limit are different questions — `attachments.to_blocks` applies the smaller
    one from the stored record and never calls this for a file over it, so
    reaching this cap means the bytes on disk disagree with what was recorded.
    """
    if not is_id(file_id):
        return None
    target = upload_dir(state_dir) / file_id
    try:
        if not target.is_file() or target.stat().st_size > config.UPLOAD_MAX_BYTES:
            return None
        return target.read_bytes()
    except OSError:
        return None


def resolve(ids: object, state_dir: str | None = None) -> list[dict[str, object]]:
    """The stored records for `ids`, dropping anything unknown.

    Silently dropping is right here: an id that does not resolve is a file that
    was never stored or has since gone, and refusing the whole message would
    lose the text of the turn over a missing thumbnail.
    """
    if not isinstance(ids, list):
        return []
    found = [metadata(str(i), state_dir) for i in ids[:MAX_PER_MESSAGE]]
    return [m for m in found if m is not None]
