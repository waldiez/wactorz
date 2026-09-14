"""The command bar: an Input that hands a dropped file to the app instead of typing it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import events
from textual.message import Message
from textual.widgets import Input

from .attachments import paths_in_paste


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

    def __init__(self, *args: Any, accept_files: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.accept_files = accept_files

    def _on_paste(self, event: events.Paste) -> None:
        """Announce a drop; any other paste falls through to Input's own handler."""
        paths = paths_in_paste(event.text) if self.accept_files else None
        if not paths:
            return  # Input's handler runs next and types it
        # Stops Input's handler, which would otherwise type the path into the bar.
        event.prevent_default()
        event.stop()
        self.post_message(self.FilesDropped(paths, event.text))
