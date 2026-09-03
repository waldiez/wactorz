"""The loop that listens to a room for a phrase, and then for a turn.

It owns the microphone for as long as it runs. That is deliberate: the turn a
person speaks begins in the same breath as the wake word, so a loop that closed
the device on detection and let something else reopen it would lose the first
words of every sentence. Detection and the turn are read from one open stream.

While it owns the device nothing else can. Asked to yield, it finishes the block
it is on, closes the stream and parks until resumed -- so the branch that records
on demand opens the microphone exactly as it would have with no loop running.
A check that waited for the phrase instead would be no check at all: the people
who need it most are the ones whose wake word is not being heard, and they cannot
tell the microphone, the model and the phrase apart by saying the phrase.
"""

from __future__ import annotations

import array
import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path

from ..stt import listener
from . import spotter

logger = logging.getLogger(__name__)


#: How long a turn may run once the phrase has woken it.
MAX_SECONDS = 15.0

#: How long a pause ends the turn.
SILENCE_SECONDS = 1.0

#: How long to wait before trying again when the microphone will not open.
#:
#: Long enough that a machine with no microphone does not fill the log, short
#: enough that plugging one in is noticed without a restart.
RETRY_SECONDS = 30.0

#: How often to look, while parked, for the microphone being handed back.
#:
#: Short: this is how long after a check finishes that the room stops being deaf,
#: and the wait costs nothing while nothing is happening.
RESUME_POLL_SECONDS = 0.05


def as_floats(pcm: bytes) -> list[float]:
    """The 16-bit samples the device gives, as the floats the model reads."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % 2])
    return [s / 32768.0 for s in samples]


class WakeLoop:
    """Listens for the phrase, and hands each woken turn to `on_turn`.

    Runs until stopped. Failures to open the device are retried rather than
    fatal: a room whose microphone is unplugged should start working when it is
    plugged back in, not when someone restarts the deployment.
    """

    def __init__(
        self,
        model_dir: Path,
        phrases: list[str],
        on_turn: Callable[[bytes], None],
        threshold: float = spotter.THRESHOLD,
    ) -> None:
        #: Called with one turn's WAV, **on the capture thread**. Anything that
        #: touches the event loop from here has to cross back deliberately --
        #: `loop.call_soon_threadsafe` -- because this is not running on it.
        self._on_turn = on_turn
        self._model_dir = model_dir
        self._phrases = phrases
        self._threshold = threshold
        self._stop = asyncio.Event()
        # Kept across yields and retries: it is twelve megabytes of weights, and
        # the phrases it was built for do not change while the loop runs.
        self._ear: spotter.Spotter | None = None
        # Threading rather than asyncio primitives: the capture thread is what
        # sets and clears these, and it is not on the event loop.
        self._yield_asked = threading.Event()
        self._device_free = threading.Event()
        self._device_free.set()

    def stop(self) -> None:
        """Ask the loop to finish, and give the microphone back.

        Returns at once. A turn already being recorded runs to its end -- up to
        `MAX_SECONDS` -- because cutting a sentence in half to shut down loses
        what someone was in the middle of saying.
        """
        self._stop.set()

    def yield_device(self, timeout: float = MAX_SECONDS + 5.0) -> bool:
        """Ask for the microphone and wait until it is actually free.

        Waits rather than returning hopefully: the caller opens the device next,
        and two streams on one microphone is the failure this exists to avoid.
        """
        self._yield_asked.set()
        return self._device_free.wait(timeout)

    def resume(self) -> None:
        """Give the microphone back to the loop."""
        self._yield_asked.clear()

    @property
    def stopping(self) -> bool:
        """Whether the loop has been asked to finish."""
        return self._stop.is_set()

    async def run(self) -> None:
        """Listen until stopped, opening the device again if it goes away."""
        try:
            while not self._stop.is_set():
                yielded = False
                try:
                    yielded = await asyncio.to_thread(self._listen_blocking)
                except listener.NoMicrophone as exc:
                    logger.warning("[wake] %s — trying again in %.0fs", exc, RETRY_SECONDS)
                except spotter.NoSpotter as exc:
                    # Not retried: a missing model or an unconvertible phrase is
                    # a configuration answer, trying again cannot change it.
                    logger.error("[wake] %s", exc)
                    return
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.exception("[wake] the listening loop failed")
                if self._stop.is_set():
                    break
                if yielded:
                    # Why it stopped, recorded by the thread that knew, rather
                    # than read back off the flag: the borrower clears that flag
                    # the moment it is done, which can happen before this line
                    # runs -- and then a hand-back would be mistaken for a
                    # failure and parked on the retry timer.
                    #
                    # Waiting on the hand-back, not on that timer: a fixed spell
                    # would leave the room deaf long after a check had finished,
                    # and could expire into reopening a device still held.
                    await self._wait_until_resumed()
                else:
                    await self._wait_before_retrying()
        finally:
            self._release_ear()

    async def _wait_until_resumed(self) -> None:
        """Park until the microphone is handed back, however long that takes."""
        while self._yield_asked.is_set() and not self._stop.is_set():
            await asyncio.sleep(RESUME_POLL_SECONDS)

    def _release_ear(self) -> None:
        """Drop the model and the keywords file it was given."""
        if self._ear is not None:
            self._ear.close()
            self._ear = None

    async def _wait_before_retrying(self) -> None:
        """Pause, unless asked to stop while pausing."""
        try:
            await asyncio.wait_for(self._stop.wait(), RETRY_SECONDS)
        except asyncio.TimeoutError:
            # Spelled this way on purpose: the two are one object from 3.11 and
            # separate on 3.10, which this still supports.
            return

    def _listen_blocking(self) -> bool:
        """Own the microphone, waking on the phrase, until asked to stop.

        Answers whether it stopped because the device was asked for, which is the
        one caller that hands it back rather than going away.
        """
        # Before the model: loading is twelve megabytes of weights, and a machine
        # with no sound device would pay it on every retry only to be told what
        # this line says immediately.
        if not listener.AUDIO:
            raise listener.NoMicrophone("sounddevice not installed — pip install 'wactorz[host]'")
        if self._yield_asked.is_set():
            # Before opening, not only while running: a request that arrived
            # while this was parked would otherwise be answered by opening the
            # device the borrower has just been handed, and closing it a block
            # later once the flag was noticed.
            return True
        if self._ear is None:
            self._ear = spotter.Spotter(self._model_dir, self._phrases, self._threshold)
        ear = self._ear
        # Whatever was half-heard before the device went away is not part of what
        # is said after it comes back.
        ear.forget()
        block_frames = int(listener.RATE * listener.BLOCK_SECONDS)
        try:
            stream = listener.sounddevice.RawInputStream(
                samplerate=listener.RATE, channels=1, dtype="int16", blocksize=block_frames
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            raise listener.NoMicrophone(f"could not open the microphone: {exc}") from exc

        try:
            with stream:
                self._device_free.clear()
                logger.info("[wake] Listening for %s", " / ".join(self._phrases))
                floor = listener.room_floor(stream, block_frames)
                threshold = listener.threshold_over(floor)
                while not self._stop.is_set() and not self._yield_asked.is_set():
                    block = bytes(stream.read(block_frames)[0])
                    said = ear.hears(as_floats(block))
                    if not said:
                        continue
                    logger.info("[wake] Woken by %r", said)
                    pcm = listener.gather_turn(
                        stream, block_frames, threshold, MAX_SECONDS, SILENCE_SECONDS
                    )
                    if pcm:
                        self._on_turn(listener.as_wav(pcm))
                # Safe to read here, where it is not in `run`: the borrower is
                # still blocked on `_device_free`, which the `finally` below has
                # not set yet, so it cannot have handed the device back.
                return self._yield_asked.is_set()
        finally:
            # After the stream is closed, not before: whoever is waiting opens
            # the device the moment this is set.
            self._device_free.set()
