"""Playback through this machine's own speakers, for ``WACTORZ_TTS=host``.

The other branches hand audio to a browser, which has a sound card and a person
in front of it. This one has neither: the answer comes out of the machine
Wactorz runs on, into whatever room that is.

PortAudio is spoken to through ``sounddevice`` (``pip install wactorz[host]``),
which carries it inside the wheel on Windows and macOS. On Linux it is a system
package, so the import can succeed and the device still be missing -- both are
reported the same way, because to whoever set this up they are the same problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import threading
import wave
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

try:  # optional dependency — the module must decode more than one kind of audio
    import miniaudio

    DECODES = True
except (ImportError, OSError):
    DECODES = False

try:  # optional dependency — the module must import without a sound card
    import sounddevice

    AUDIO = True
except (ImportError, OSError):
    # OSError as well as ImportError: the Linux wheel carries no PortAudio, so
    # importing it without the system library raises rather than failing to find
    # the module at all. The name stays bound either way — every use sits behind
    # the AUDIO flag, and an attribute that vanishes with the dependency cannot
    # be patched in tests.
    sounddevice: Any = None
    AUDIO = False


#: The kind of audio that needs nothing beyond the standard library. Anything
#: else is decoded by miniaudio, which the host extra carries.
PLAYABLE = "audio/wav"

#: How long past the end of the audio to wait before giving up on the device.
#: A wedged sound card must not hold the turn that asked for the speech.
MARGIN = 10.0

#: One room, one voice. Two turns answering at once would otherwise talk over
#: each other, or take a device that allows a single writer and fail the second.
#: Built at import: since 3.10 a lock takes the running loop when it is first
#: awaited rather than when it is made, so this binds to whichever loop uses it.
_speaking = asyncio.Lock()

#: How much audio is handed to the device at a time. Speech is written in
#: pieces rather than in one call so that stopping is possible at all: a single
#: write blocks until the whole sentence has played, and neither cancelling the
#: task nor aborting the stream releases it.
CHUNK_SECONDS = 0.1

#: Set to cut the current speech short. Checked between pieces, so the room goes
#: quiet within one of them rather than at the end of the sentence.
_stopped = threading.Event()

#: Turns that are going to speak but have not started. See `about_to_speak`.
_intending = 0


class NoSpeakers(RuntimeError):
    """Raised when this machine cannot play audio."""


def is_speaking() -> bool:
    """Whether this machine is talking right now.

    Asked by the microphone on the same machine: a room where both branches are
    on has one device answering into the space the other is listening to, and a
    reply captured as a question is a turn that asks itself, forever, billed by
    the token.
    """
    return _speaking.locked() or _intending > 0


@contextlib.contextmanager
def about_to_speak() -> Iterator[None]:
    """Count a turn as speaking from before its audio exists.

    Making the speech takes as long as the synthesiser takes, and until it is
    made there is nothing holding the playback lock. A turn that started
    listening in that window would record the room and then the reply as it
    began -- the machine asking itself, which is what the guard is for.

    Counted rather than flagged: two turns can be in flight, and the first to
    finish must not declare the room quiet while the second is still speaking.
    """
    global _intending
    _intending += 1
    try:
        yield
    finally:
        _intending -= 1


def silence() -> None:
    """Stop whatever is being said, now.

    For a turn someone cancelled: the interface says it stopped, and a machine
    still talking to the room disagrees with it out loud.

    Asking the device to stop does not work: ``sounddevice.stop()`` reaches only
    the convenience playback helpers and says so in its own docstring, and
    aborting the stream leaves the thread inside ``write`` exactly where it was.
    What does work is not being inside a long write in the first place, which is
    why the speech is handed over in pieces and this only has to say so.
    """
    _stopped.set()


def available() -> bool:
    """Whether there is something to play through."""
    if not AUDIO:
        return False
    try:
        return any(device["max_output_channels"] > 0 for device in sounddevice.query_devices())
    except Exception:  # pylint: disable=broad-exception-caught
        # Any failure to enumerate is an absent device as far as a caller is
        # concerned, and the reasons are a portaudio matter rather than ours.
        return False


async def play(audio: bytes) -> None:
    """Play one WAV, returning when it has finished.

    Off the event loop, because playback lasts as long as the speech does and
    the loop has a dashboard to keep answering in the meantime. Waits for any
    speech already going out, then bounded by how long this audio should take:
    a device that stops accepting samples otherwise holds the turn for ever.
    """
    if not AUDIO:
        raise NoSpeakers("sounddevice not installed — pip install 'wactorz[host]'")
    samples, rate, channels = _decode(audio)
    seconds = len(samples) / (rate * channels * 2)
    async with _speaking:
        # Cleared here rather than when the speech ends: a stop that arrives
        # between two turns belongs to the one that has finished, and would
        # otherwise silence the next thing said.
        _stopped.clear()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_play_blocking, samples, rate, channels),
                seconds + MARGIN,
            )
        except asyncio.TimeoutError as exc:
            # Spelled this way on purpose: the two names are one object from 3.11
            # and separate classes on 3.10, which this still supports, so the
            # builtin catches nothing there and the timeout escapes.
            #
            # The thread is left to finish on its own: it is blocked inside
            # portaudio, where there is nothing to cancel it with. Saying so
            # matters more than pretending the room went quiet.
            raise NoSpeakers("the sound device stopped accepting audio") from exc


def can_play(content_type: str) -> bool:
    """Whether audio of this kind can be turned into samples here."""
    return content_type == PLAYABLE or DECODES


def _decode(audio: bytes) -> tuple[bytes, int, int]:
    """Take the samples, their rate and their shape out of whatever this is.

    A 16-bit WAV is read by the standard library, which every install has.
    Anything else -- the MP3 the in-process synthesiser answers with, most of
    all -- goes to miniaudio, so the branch is not limited to the one backend
    that happens to answer in the simplest container.
    """
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            if source.getsampwidth() == 2:
                return (
                    source.readframes(source.getnframes()),
                    source.getframerate(),
                    source.getnchannels(),
                )
    except wave.Error:
        pass  # not a WAV at all, which miniaudio may still know what to do with

    if not DECODES:
        raise NoSpeakers(
            "only 16-bit WAV can be played without miniaudio — pip install 'wactorz[host]'"
        )
    try:
        decoded = miniaudio.decode(audio, output_format=miniaudio.SampleFormat.SIGNED16)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise NoSpeakers(f"cannot decode this audio: {exc}") from exc
    return bytes(decoded.samples), decoded.sample_rate, decoded.nchannels


def _play_blocking(samples: bytes, rate: int, channels: int) -> None:
    """Write the samples to the default output and wait for the end.

    Written as raw bytes rather than through an array: that is the shape they
    arrive in, and converting would put numpy between this and the speakers for
    no gain -- it is not a dependency this project otherwise has.
    """
    step = max(1, int(rate * CHUNK_SECONDS)) * channels * 2
    try:
        stream = sounddevice.RawOutputStream(samplerate=rate, channels=channels, dtype="int16")
        with stream:
            for start in range(0, len(samples), step):
                if _stopped.is_set():
                    return
                stream.write(samples[start : start + step])
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise NoSpeakers(f"playback failed: {exc}") from exc
