"""The microphone on this machine, for ``WACTORZ_STT=host``.

The other branches are handed audio a browser captured, which means a person
chose to send it. This one hears the room, so it has to decide for itself when
someone has finished speaking: there is no button to let go of.

That decision is made on loudness against the room's own noise floor rather than
a fixed number. A quiet laptop microphone and a desk beside a fan differ by more
than any constant would survive, and a threshold set too high hears nothing at
all while reporting no error.

Audio is gathered and then transcribed, rather than streamed as it arrives.
Nobody is watching words appear on this branch -- the machine answers aloud when
the sentence is done -- so the simpler shape is worth more than the latency.
"""

from __future__ import annotations

import array
import asyncio
import io
import logging
import wave
from typing import Any

logger = logging.getLogger(__name__)

try:  # optional dependency — the module must import without a microphone
    import sounddevice

    AUDIO = True
except (ImportError, OSError):
    # OSError as well: the Linux wheel carries no PortAudio, so importing it
    # without the system library raises rather than failing to find the module.
    # The name stays bound either way — every use sits behind the AUDIO flag,
    # and an attribute that vanishes with the dependency cannot be patched in
    # tests.
    sounddevice: Any = None
    AUDIO = False

#: What the recognisers want, and what the capture is opened at.
RATE = 16000

#: How much audio is examined at a time when deciding whether anyone is talking.
BLOCK_SECONDS = 0.05

#: How long the room is listened to before anything is said, to learn its floor.
FLOOR_SECONDS = 0.4

#: How far above the room's own noise a block must be to count as speech. Low
#: enough for a laptop microphone, which is quieter than most people expect.
OVER_FLOOR = 2.5

#: The quietest a block can be and still count, however silent the room is. Stops
#: a perfectly quiet room from making its own hiss into speech.
FLOOR_MINIMUM = 0.002

#: How long a silence ends the turn, and how long one turn may run regardless.
SILENCE_SECONDS = 1.2
MAX_SECONDS = 20.0

#: How long past the end of a turn to wait before giving up on the device. A
#: microphone that stops delivering must not hold the request that asked it to
#: listen, and there is nothing to interrupt a blocked read with.
MARGIN = 10.0

#: One room, one microphone. Two turns listening at once take the same words
#: twice, and on a device that allows a single reader the second simply fails.
_listening = asyncio.Lock()

#: Whatever holds the microphone open between turns, or nothing.
#:
#: The loop that waits for a phrase owns the device for as long as it runs, so a
#: turn recorded on demand has to ask for it. Kept here rather than known to
#: either caller: the device is what they share, and a module that imported the
#: other to ask would close a circle -- the loop already reads this one.
_owner: Any = None


def claim(owner: Any) -> None:
    """Say that `owner` holds the microphone between turns."""
    # One device, so its holder is named in one place.
    global _owner
    _owner = owner


def release() -> None:
    """Say that nothing holds it any more."""
    global _owner
    _owner = None


class NoMicrophone(RuntimeError):
    """Raised when this machine cannot listen.

    The reasons are named here rather than written at each raise: the same two
    are reached from the branch that records on demand and from the loop that
    waits for a phrase, and a message that differs between them describes the
    caller rather than the machine.
    """

    @classmethod
    def uninstalled(cls) -> NoMicrophone:
        """The optional dependency that opens a device is absent."""
        return cls("sounddevice not installed — pip install 'wactorz[host]'")

    @classmethod
    def would_not_open(cls, exc: object) -> NoMicrophone:
        """A device is there in principle and refused in practice."""
        return cls(f"could not open the microphone: {exc}")

    @classmethod
    def still_in_use(cls) -> NoMicrophone:
        """Whatever holds the device did not let go of it when asked."""
        return cls("the microphone is still in use by the wake word")


#: Whether a microphone was found, once asked. Enumerating devices talks to the
#: sound system, which is too slow to do on the loop for every request, and
#: hardware does not come and go over the life of a process.
_found: bool | None = None


def available() -> bool:
    """Whether there is something to listen through."""
    # Asked before the cache, not through it: whether the library is there is
    # already known, and caching that answer would outlive anything that changed
    # it. Only the device enumeration is worth remembering.
    if not AUDIO:
        return False
    global _found
    if _found is not None:
        return _found
    try:
        _found = any(device["max_input_channels"] > 0 for device in sounddevice.query_devices())
    except Exception:  # pylint: disable=broad-exception-caught
        # Any failure to enumerate is an absent microphone as far as a caller is
        # concerned, and the reasons are a portaudio matter rather than ours.
        _found = False
    return _found


def loudness(block: bytes) -> float:
    """How loud one block of 16-bit samples is, as a fraction of full scale."""
    samples = array.array("h")
    samples.frombytes(block[: len(block) - len(block) % 2])
    if not samples:
        return 0.0
    total = sum(float(s) * float(s) for s in samples)
    return (total / len(samples)) ** 0.5 / 32768.0


async def listen(
    max_seconds: float = MAX_SECONDS, silence_seconds: float = SILENCE_SECONDS
) -> bytes:
    """Record until whoever is speaking stops, and answer with a WAV.

    Off the event loop: capture runs for as long as someone talks, and the loop
    has a dashboard to keep answering meanwhile.
    """
    if not AUDIO:
        raise NoMicrophone.uninstalled()
    async with _listening:
        owner = _owner
        if owner is not None and not await asyncio.to_thread(owner.yield_device):
            # Refused rather than opened anyway: two streams on one device is
            # the failure asking exists to avoid, and waiting for ever would
            # hang whoever asked instead of telling them.
            owner.resume()
            raise NoMicrophone.still_in_use()
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_listen_blocking, max_seconds, silence_seconds),
                max_seconds + MARGIN,
            )
        finally:
            # Whatever happened to the turn: a failed one that kept the device
            # would leave the room deaf until a restart.
            if owner is not None:
                owner.resume()


def _listen_blocking(max_seconds: float, silence_seconds: float) -> bytes:
    """Open the microphone and gather one turn's audio."""
    block_frames = int(RATE * BLOCK_SECONDS)
    try:
        stream = sounddevice.RawInputStream(
            samplerate=RATE, channels=1, dtype="int16", blocksize=block_frames
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise NoMicrophone.would_not_open(exc) from exc

    with stream:
        floor = room_floor(stream, block_frames)
        pcm = gather_turn(stream, block_frames, threshold_over(floor), max_seconds, silence_seconds)
    return as_wav(pcm) if pcm else b""


def threshold_over(floor: float) -> float:
    """How loud a block must be to count as speech in a room this noisy."""
    return max(floor * OVER_FLOOR, FLOOR_MINIMUM)


def gather_turn(
    stream: Any,
    block_frames: int,
    threshold: float,
    max_seconds: float,
    silence_seconds: float,
) -> bytes:
    """Read one turn from an already-open stream, and answer with its samples.

    Takes the stream rather than opening one: a wake word owns the microphone
    continuously, and closing it to reopen for the turn loses whatever was said
    in between -- which is the beginning of the sentence, since people carry
    straight on from the wake word.

    Answers with raw samples, or nothing when the room stayed quiet.
    """
    spoken: list[bytes] = []
    quiet_for = 0.0
    heard_anything = False
    for _ in range(int(max_seconds / BLOCK_SECONDS)):
        block = bytes(stream.read(block_frames)[0])
        spoken.append(block)
        if loudness(block) >= threshold:
            heard_anything = True
            quiet_for = 0.0
            continue
        quiet_for += BLOCK_SECONDS
        # Only once someone has actually said something: otherwise the turn
        # ends on the silence it opened with, before anyone drew breath.
        if heard_anything and quiet_for >= silence_seconds:
            break

    if not heard_anything:
        return b""
    # Said plainly, once per turn: a microphone that listens to a room without
    # anything in the log to show for it is worse than one that does not.
    seconds = sum(len(b) for b in spoken) / (RATE * 2)
    logger.info("[stt] Heard %.1fs from the room", seconds)
    return b"".join(spoken)


def room_floor(stream: object, block_frames: int) -> float:
    """Measure the room's own noise, so the threshold suits where it is."""
    levels = [
        loudness(bytes(stream.read(block_frames)[0]))  # type: ignore[attr-defined]
        for _ in range(max(1, int(FLOOR_SECONDS / BLOCK_SECONDS)))
    ]
    return sum(levels) / len(levels)


def as_wav(pcm: bytes) -> bytes:
    """Wrap the samples in the container the recognisers are given."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        # pylint: disable=no-member
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)
    return buffer.getvalue()
