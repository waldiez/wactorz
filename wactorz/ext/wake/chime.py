"""The sound a room hears when the machine has heard its phrase.

On the branch that hears a room nobody is looking at a screen, so being woken
has to be heard: without it, someone who said the phrase cannot tell a machine
that is listening from one that missed it, and repeats themselves into the turn.

Two rising notes, short and quiet, played beside the recording rather than
before it: people carry straight on from the phrase, and waiting for the sound
to end would cut off the start of what they say.
"""

import array
import math

from ..tts import speaker

#: The rate the notes are made at: one any output plays without converting.
RATE = 22050

#: Each note's pitch and length, in order, in hertz and seconds.
NOTES = ((660.0, 0.11), (990.0, 0.14))

#: How loud, as a share of full scale: heard across a room, not over a voice.
LEVEL = 0.18

#: How long each note takes to rise and fall, so neither starts or ends in a click.
FADE_SECONDS = 0.01


def _notes() -> bytes:
    samples = array.array("h")
    fade = int(RATE * FADE_SECONDS)
    for pitch, seconds in NOTES:
        count = int(RATE * seconds)
        for i in range(count):
            envelope = min(1.0, i / fade, (count - 1 - i) / fade)
            value = LEVEL * envelope * math.sin(2 * math.pi * pitch * i / RATE)
            samples.append(int(value * 32767))
    return samples.tobytes()


#: The sound itself, made once.
SOUND = _notes()


def chime() -> None:
    """Let the room hear it was heard, without waiting for the sound to end."""
    speaker.play_aside(SOUND, RATE)
