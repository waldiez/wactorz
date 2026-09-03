"""Waking on a spoken phrase.

Runs only where there is a microphone to own: the `host` branch. Every other
branch records in a browser or not at all, so there is no device here for a loop
to hold and nothing for a phrase to interrupt.

Whether it runs is a setting rather than a restart, so this is reconciled -- told
"make the world match the settings" -- rather than started once. The same call
serves startup and a change made while running.
"""

import asyncio
import logging
from pathlib import Path

from aiohttp import web

from ... import config
from ...core import voice_settings
from ..stt import listener
from . import loop

logger = logging.getLogger(__name__)

#: The loop that owns the microphone, while one does.
_running: loop.WakeLoop | None = None

#: What it is running as, so a change can be told from a repeat.
_task: asyncio.Task[None] | None = None

#: Whether the missing model has been mentioned, so it is mentioned once.
_said_no_model = False

#: Turns being routed, held so the loop does not collect them mid-flight.
#:
#: The event loop keeps only a weak reference to a task, so one nothing else
#: holds can be collected before it finishes -- rarely, silently, and by losing
#: exactly the turn someone spoke.
_routing: set[asyncio.Task[None]] = set()


def setup(app: web.Application) -> None:
    """Start listening for the phrase if this deployment is set to."""
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)


async def _on_startup(_app: web.Application) -> None:
    await reconcile()


async def _on_cleanup(_app: web.Application) -> None:
    await shutdown()


def wanted() -> bool:
    """Whether a phrase should be waking this deployment right now."""
    return voice_settings.waking() == "on" and voice_settings.listening() == "host"


async def reconcile() -> None:
    """Start or stop the loop so that it matches the settings.

    Safe to call repeatedly: a deployment already doing what the settings ask is
    left alone, because starting a second loop would be a second claim on one
    device and the newer would spend its life failing to open what the older
    holds.
    """
    # One loop and one device, so the holder is named in one place.
    global _running, _task
    if wanted() == (_running is not None):
        return
    if _running is not None:
        await shutdown()
        return

    if not config.WAKE_MODEL_DIR:
        # Once, not per settings change: the answer does not differ between two
        # of them, and a line repeated on every save buries the one that mattered.
        global _said_no_model
        if not _said_no_model:
            _said_no_model = True
            logger.warning(
                "[wake] WACTORZ_WAKE is on but WACTORZ_WAKE_MODEL names no model directory"
            )
        return

    # Bound here, where there is one: the callback runs on the capture thread,
    # which has no loop of its own to ask for.
    on_this_loop = asyncio.get_running_loop()

    def heard(clip: bytes) -> None:
        on_this_loop.call_soon_threadsafe(_route_from_the_room, clip)

    _running = loop.WakeLoop(Path(config.WAKE_MODEL_DIR), list(config.WAKE_WORDS), heard)
    # Claimed before it starts: whoever records on demand has to find an owner to
    # ask, and the window between starting and claiming is one where two callers
    # would both believe the device was theirs.
    listener.claim(_running)
    _task = asyncio.create_task(_running.run())


async def shutdown() -> None:
    """Stop the loop, and give the microphone back to whatever wants it."""
    global _running, _task
    if _running is None:
        return
    _running.stop()
    listener.release()
    _running = None
    if _task is not None:
        # Awaited rather than abandoned: it is holding a device, and a process
        # that exits without waiting leaves the stream open behind it.
        await _task
        _task = None


def _route_from_the_room(clip: bytes) -> None:
    """Start routing one woken turn. Runs on the event loop, not the capture thread."""
    # Imported here to break the circle: the recognition extension reaches back
    # into the web layer, which is built on the extensions, so a module-scope
    # import either way round cannot be resolved at start.
    from ..stt import route_heard_clip

    task = asyncio.ensure_future(route_heard_clip(clip, source="wake"))
    _routing.add(task)
    task.add_done_callback(_routing.discard)
