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

logger = logging.getLogger(__name__)

try:  # optional dependency — the module must import without a microphone
    import sounddevice

    AUDIO = True
except (ImportError, OSError):
    # OSError as well: the Linux wheel carries no PortAudio, so importing it
    # without the system library raises rather than failing to find the module.
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


class NoMicrophone(RuntimeError):
    """Raised when this machine cannot listen."""


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
        raise NoMicrophone("sounddevice not installed — pip install 'wactorz[host]'")
    async with _listening:
        return await asyncio.wait_for(
            asyncio.to_thread(_listen_blocking, max_seconds, silence_seconds),
            max_seconds + MARGIN,
        )


def _listen_blocking(max_seconds: float, silence_seconds: float) -> bytes:
    """Open the microphone and gather one turn's audio."""
    block_frames = int(RATE * BLOCK_SECONDS)
    try:
        stream = sounddevice.RawInputStream(
            samplerate=RATE, channels=1, dtype="int16", blocksize=block_frames
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise NoMicrophone(f"could not open the microphone: {exc}") from exc

    spoken: list[bytes] = []
    with stream:
        floor = _room_floor(stream, block_frames)
        threshold = max(floor * OVER_FLOOR, FLOOR_MINIMUM)
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
    return _as_wav(b"".join(spoken))


def _room_floor(stream: object, block_frames: int) -> float:
    """Measure the room's own noise, so the threshold suits where it is."""
    levels = [
        loudness(bytes(stream.read(block_frames)[0]))  # type: ignore[attr-defined]
        for _ in range(max(1, int(FLOOR_SECONDS / BLOCK_SECONDS)))
    ]
    return sum(levels) / len(levels)


def _as_wav(pcm: bytes) -> bytes:
    """Wrap the samples in the container the recognisers are given."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm)
    return buffer.getvalue()
