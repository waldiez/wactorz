"""The command bar: an Input that hands a dropped file to the app instead of typing it."""

from pathlib import Path
from typing import Any

from textual import events
from textual.message import Message
from textual.widgets import Input

from .attachments import paths_in_paste
from .completion import Offers


class CommandInput(Input):
    """The command bar. A paste that names files is announced rather than typed.

    Only announced, never decided here: whether the paths exist needs the disk,
    and a paste event is no place to wait on it. The app checks, and types the
    text into the bar after all when the paths turn out not to be files.
    """

    class FilesDropped(Message):
        """A paste that names files by absolute path, as a drop onto a terminal does."""

        def __init__(self, paths: list[Path], text: str) -> None:
            super().__init__()
            self.paths = paths
            self.text = text

    def __init__(
        self, *args: Any, accept_files: bool = True, offers: Offers | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.accept_files = accept_files
        self._offers = offers
        # Where ↑ has got to in what was typed, and what was in the bar before it.
        self._back = 0
        self._draft = ""

    def key_up(self) -> None:
        """↑ — the previous thing typed."""
        typed = self._offers.typed if self._offers else []
        if self._back < len(typed):
            self._draft = self.value if self._back == 0 else self._draft
            self._back += 1
            self.value = typed[-self._back]

    def key_down(self) -> None:
        """↓ — the next thing typed, ending on what was in the bar."""
        typed = self._offers.typed if self._offers else []
        if self._back:
            self._back -= 1
            self.value = typed[-self._back] if self._back else self._draft

    def _on_paste(self, event: events.Paste) -> None:
        """Announce a drop; any other paste falls through to Input's own handler."""
        paths = paths_in_paste(event.text) if self.accept_files else None
        if not paths:
            return  # Input's handler runs next and types it
        # Stops Input's handler, which would otherwise type the path into the bar.
        event.prevent_default()
        event.stop()
        self.post_message(self.FilesDropped(paths, event.text))
