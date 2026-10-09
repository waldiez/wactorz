# User guide

How to use Reachy once it is running. Every example is typed into the Wactorz dashboard
chat, prefixed with `@reachy-mini`; during a voice conversation you say the same words
without the prefix.

Send `@reachy-mini help` at any time for the built-in guide, or `help movement`,
`help voice`, `help camera`, `help connection`, `help volume`, `help home` for one topic.
Help works even when the robot is disconnected.

## How Reachy decides what to do

1. **Fixed phrases** run immediately without a language model: `wake up`, `nod`,
   `look left`, `take a photo`, `stop talking`, `start conversation`, and the others below.
2. **Other robot or home requests** ("look up slowly and wiggle your antennas") are turned
   into robot commands by the language model.
3. **Everything else** (questions, calendar, weather, other agents) goes to Wactorz's
   main agent. Reachy speaks its answer and the full text appears in the chat.
   Start with `ask Wactorz` to send something straight there.

Every command answers in the chat, including failures, so a robot that did not move never
looks like one that did.

## Movement

| Say or type | What happens |
| --- | --- |
| `wake up` | Head and antennas come to the ready position, with a short sound. |
| `go to sleep` | A sleepy droop. The motors stay on; it is a pose, not power-off. |
| `look left` / `look right` / `look up` / `look down` | Head turns. |
| `turn left` / `turn right 90 degrees` | The body rotates; 45 degrees unless you give an angle. |
| `turn around`, `face me` | Turns to face away, and back. |
| `nod`, `shake your head`, `dance`, `wiggle your antennas`, `look curious` | Built-in gestures. |
| `do a happy gesture`, `act sleepy` | Planned by the language model from smaller moves. |
| `stop` | Stops speaking. The structured command `{"cmd": "stop"}` also holds the head where it is. |
| `enable motors` / `go limp` | Motor power on, or off so the head can be posed by hand. |

Head and body angles are limited in the code to the robot's safe range whatever is asked.

### Idle motion

Reachy stays completely still between commands unless you turn idle motion on:

| Say | Effect |
| --- | --- |
| `alive` / `back to normal` | Gentle breathing, drifting gaze, an occasional larger "attract" move. |
| `calm down` | Smaller and slower. |
| `antennas only` | Antennas move; head and body stay still. Good near people's faces. |
| `showtime` | Bigger and more frequent, for a busy room. |
| `stop moving` | Completely still again. Commands still work. |

Greek works too: `ηρέμησε`, `μόνο τις κεραίες`, `μη κουνιέσαι`, `πιο ζωηρά`, `κανονικά`.
Idle motion pauses whenever Reachy is carrying out a command or speaking.

## Speech

| Say or type | What happens |
| --- | --- |
| `say Good morning everyone` | Speaks exactly those words. |
| `whisper hello` | Speaks quietly. |
| `say welcome loudly` | Speaks at presenter volume. |
| `stop talking`, `be quiet`, `shut up` | Cuts off the current sentence. |
| `speak louder` / `lower your voice` | Volume up or down a step. |
| `whisper`, `normal volume`, `louder`, `presenter mode` | Named volume levels (70, 85, 93, 100). |
| `mute` / `unmute` | Silences the speaker until unmuted. |

Reachy speaks English and Greek, choosing a suitable voice for the text. Emoji, links and
formatting are shown in the chat but not read aloud.

## Voice input

Voice input needs speech recognition set up (see
[Getting started › step 8](getting-started.md#8-optional-talk-to-reachy-by-voice)).
If it is not, Reachy says what is missing instead of listening.

**One question (push-to-talk):** `listen and ask Wactorz`. Reachy records five seconds,
transcribes them, sends the words to Wactorz and speaks the answer.

**A conversation:** `start conversation` (or `start listening`).

- Speak normally after Reachy has finished speaking. A short pause ends your turn.
- What Reachy heard appears in the chat under Reachy, followed by the answer.
- Robot requests work in conversation too: "turn left", "what do you see?",
  "turn on the kitchen light".
- **End it** by saying "goodbye", "stop listening" or "that's all", or by typing
  `stop conversation`.
- Reachy finishes its sentence before listening again. To be able to talk over it, start
  with `start conversation with interruption`; this is experimental, because on some robots
  the microphone hears Reachy's own speaker.
- If the conversation ends by itself, because nobody spoke for the configured time or
  several turns in a row failed, Reachy says why in the chat.

Recognition works best within about a metre, facing the robot, one person at a time.

## Camera

| Say or type | What happens |
| --- | --- |
| `take a photo` | Captures one frame. The chat confirms its size; programs receive the image itself in the command result. Nothing is saved to disk. |
| `what do you see?` | Describes the view in one sentence, out loud. |
| `look closer`, `tell me more` | A fuller description of the same view. |
| `look around` | Turns to several angles and describes the room. |
| `what's behind you?` | Turns to look behind, describes it, and stays turned. `face me` to return. |

Descriptions are made by your language model, so the image is sent to that provider. The
model has to accept images.

## Home Assistant and other agents

With Home Assistant connected to Wactorz:

```text
@reachy-mini turn on the living-room light
@reachy-mini ask Wactorz what's on my calendar today?
@reachy-mini when the front door opens, wake up
```

Device control goes through Wactorz's own Home Assistant agents; Reachy never holds a Home
Assistant token. A "when … do …" request with a robot reaction creates a lasting rule on
Reachy, kept across restarts; `stop reacting to the light` removes it.

## Status and recovery

| Say or type | What happens |
| --- | --- |
| `health` | Connection, motor faults the robot has reported, whether the voice boost is on. |
| `reconnect` | Re-opens the link, for example after powering the robot on. |
| `reconnect force` | Re-opens it even if it looks connected. |
| `diagnostics` | Checks software versions and makes a small test move. |
| `enable debug` / `disable debug` | Show or hide the internal step list behind each answer. |

Reachy Mini does not report its battery level to software. Watch the LED on its base:
green is fine, orange means soon, red means charge now.

## For programs and automations

Reachy listens on MQTT, so scripts and other agents can drive it without the chat:

```text
topic: custom/reachy/cmd
payload: {"cmd": "pose", "yaw": 30, "duration": 0.6}
```

It publishes its state on `custom/reachy/state` and every command's result on
`custom/reachy/events`. The full command list is in the
[catalogue reference](../catalogue-reachy-mini.md#structured-commands).
