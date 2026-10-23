"""Keep the model's action blocks out of a reply while it streams.

Main's model acts by writing ``<spawn>``, ``<delete>`` and ``<delegate>``
blocks into its reply. They are instructions for main, not text for the user,
and main runs them only once the whole reply is in. A streamed reply reaches the
user chunk by chunk before that, so the blocks are taken out on the way: a
block's opening tag can arrive split over two chunks, and its body over many.

The non-streaming path removes the same blocks from the finished reply with a
regular expression; this filter gives the same text for any way the reply is
cut into chunks.
"""

ACTION_TAGS = ("spawn", "delete", "delegate")
"""The blocks main runs from its model's reply."""

_OPENINGS = {f"<{tag}>": f"</{tag}>" for tag in ACTION_TAGS}


class ActionBlockFilter:
    """Remove action blocks from a reply that arrives in chunks.

    Text outside a block is passed on as soon as it cannot be the start of an
    opening tag, so prose with a ``<`` in it is held back by one chunk at most.
    A block the reply never closes is passed on whole at :meth:`flush`, as the
    non-streaming path leaves one in place: a reply cut short stays visible
    rather than disappearing.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._closing: str | None = None
        # Where to look for the closing tag next, so a long block is not
        # searched from its start on every chunk.
        self._scan_from = 0

    def feed(self, chunk: str) -> str:
        """Take the next chunk; return the text that can be shown now."""
        self._pending += chunk
        shown: list[str] = []
        while self._pending:
            if self._closing is not None:
                end = self._pending.find(self._closing, self._scan_from)
                if end < 0:
                    self._scan_from = max(0, len(self._pending) - len(self._closing) + 1)
                    break
                self._pending = self._pending[end + len(self._closing) :]
                self._closing = None
                continue
            start, opening = _first_opening(self._pending)
            if opening is None:
                keep = _partial_opening_length(self._pending)
                shown.append(self._pending[: len(self._pending) - keep])
                self._pending = self._pending[len(self._pending) - keep :]
                break
            shown.append(self._pending[:start])
            self._pending = self._pending[start:]
            self._closing = _OPENINGS[opening]
            self._scan_from = len(opening)
        return "".join(shown)

    def flush(self) -> str:
        """End the reply; return what was held back, an unclosed block included."""
        rest = self._pending
        self._pending = ""
        self._closing = None
        self._scan_from = 0
        return rest


def _first_opening(text: str) -> tuple[int, str | None]:
    """The position and tag of the earliest complete opening tag in ``text``."""
    found: tuple[int, str | None] = (-1, None)
    for opening in _OPENINGS:
        at = text.find(opening)
        if at >= 0 and (found[1] is None or at < found[0]):
            found = (at, opening)
    return found


def _partial_opening_length(text: str) -> int:
    """How long a tail of ``text`` could still grow into an opening tag."""
    start = text.rfind("<")
    if start < 0:
        return 0
    tail = text[start:]
    if any(opening.startswith(tail) for opening in _OPENINGS):
        return len(tail)
    return 0
