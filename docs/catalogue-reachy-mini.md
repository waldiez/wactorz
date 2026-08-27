# Reachy Mini agent

`reachy-mini` controls a Reachy Mini from Wactorz. Use it for wake/sleep, head pose,
antennas, gaze, speech, expressive gestures, and optional Home Assistant actions.

## Hardware setup

Reachy Mini Wireless:

1. Power on the robot.
2. Put the robot and the Wactorz host on the same WiFi network.
3. Make sure the network does not block local device discovery.
4. Stop any Hugging Face app running on the robot before Wactorz connects.

Reachy Mini Lite:

1. Connect the robot over USB.
2. Start the local daemon before spawning the agent:

```bash
reachy-mini-daemon -p <serial_port>
```

## Install dependencies

The catalogue installer can install recipe dependencies on first spawn. To install them
manually:

```bash
pip install "reachy-mini==1.8.4" numpy edge-tts webrtcvad-wheels
```

`edge-tts` is required for the `say` command (speech synthesis); the other
commands work without it.

**Optional:** install `ffmpeg` (a system binary, not a pip package) on the host
if the robot's speech comes out too quiet. It is used only to boost the TTS
loudness by roughly 3-4x. Without it, `say` still works - it plays the raw,
quieter audio and says so once per session. The Home Assistant add-on image does
not carry it, since it serves this one optional agent; install it on the host
running Wactorz if you want the louder speech.

## Spawn

```text
@catalog spawn reachy-mini
```

Confirm it is running:

```text
/agents
```

If the robot was disconnected and reconnected:

```text
/agents restart reachy-mini
```

## Pin a Wireless host

The Reachy SDK usually auto-detects the robot. If discovery is unreliable, publish the host
once, then say `reconnect` to apply it (no restart needed):

```text
topic: custom/reachy/config
payload: {"robot_host": "192.168.1.42"}
```

Use the robot's IP address or hostname. The current host is reported in
`custom/reachy/state` as `robot_host`.

## Reconnect after the robot was off

The agent connects once at spawn. If the robot was powered off then — or the daemon
link drops — the agent stays up and refuses robot commands with a `reachy not
connected` message. Power the robot on and say:

```text
@reachy-mini reconnect
```

`connect`, `try again`, and `retry` work too, as does publishing `{"cmd": "reconnect"}`
to `custom/reachy/cmd`. It re-runs the same connection ladder `setup()` uses and brings
the robot back up (volume sync, motor torque, wake), reporting what actually happened —
a failed attempt says so rather than claiming success. Use `reconnect force` to re-open
a link that looks alive but isn't behaving.

Because the ladder reads the *current* config, a `custom/reachy/config` publish followed
by `reconnect` re-targets a new host or mode without a restart. Values set in `.env`
(`REACHY_ROBOT_HOST`, `REACHY_CONNECTION_MODE`, `REACHY_MEDIA_BACKEND`) are read only at
spawn, so changing those still needs a restart.

## Hardware warnings and what Reachy can tell you about itself

Say `are you overheating` (or `health`, `how are you feeling`, `battery`) for what the
robot reports about its own condition.

**Motor faults, including overheating, are watched automatically.** The daemon reads the
servos' hardware-error byte several times a second and decodes overheating, overload,
electrical-shock and input-voltage faults — but it writes them to its own log and nowhere
the SDK can read, not even into the status it serves. Wactorz therefore follows the
daemon's log stream (`/logs/ws/daemon`, the same source the official control app shows its
overheating warning from) and raises each fault in chat with what to do about it. The same
fault on the same motor is repeated at most every five minutes, because the daemon re-reads
the motors continuously and would otherwise produce a warning per read. The watch starts on
connect and reconnects on its own, since the stream ends whenever the daemon restarts.

**Only the wireless robot serves that stream** — the daemon mounts the route behind its
wireless flag — so a Lite over USB has no fault watch. `health` says so explicitly rather
than reporting silence, which would otherwise read as "nothing wrong".

**There is no battery level, and there is no way to add one.** Pollen state it plainly in
their FAQ: "We do not have the possibility to check the battery status, that's a known
limitation of the design. We only have the led indication for low battery when it's time to
charge it (green -> orange -> red)." The charge state is wired to that LED on the foot and
reaches no software at all, so `health` says so outright rather than leaving an absent number
to be read as a full charge. **Watch the LED, not the dashboard**: green is fine, orange means
soon, red means charge it now.

**An `Input Voltage Error` from a motor is not a fault here** and is never raised as a warning.
Reachy Mini runs its servos above their own protection threshold deliberately — Pollen's FAQ
says "We are using a higher voltage on Reachy Mini, it's on purpose" — so it appears on healthy
robots. It is written to the Wactorz log and left there; warning about it would train you to
ignore the faults that matter.

The one temperature available is the IMU's own, reported by `health` when the robot has an
IMU. It is the inertial chip, not a motor, so treat it as the robot's internal ambient — the
thing that actually overheats reports through the fault watch above.

## Choose a connection mode

By default the SDK auto-detects: it probes `localhost` first, then the robot. If you
run the robot wirelessly and do **not** have the Reachy Mini control app open, pin
`network` so the agent connects straight to the robot and skips the localhost probe:

```text
topic: custom/reachy/config
payload: {"connection_mode": "network"}
```

When you do want the Reachy Mini control app or its **simulator** (for example a shared
demo, or working with no physical robot), pin `local` instead:

```text
topic: custom/reachy/config
payload: {"connection_mode": "local"}
```

After publishing, say `reconnect` to apply the new mode. You can also set
`REACHY_CONNECTION_MODE=network` (or `local`) in the environment, which is read at spawn
and so needs a restart. The active mode is reported in `custom/reachy/state` as
`connection_mode`. This only selects *where* the agent connects — speaker/host audio
routing is set separately by `media_backend`.

> If the robot talks but does not move, the agent likely reached the robot's media
> stream but not its motor control. Use `network` mode (no control app), or make sure
> the control app / simulator is running for `local` mode.

## Use it

Plain English works for normal use:

```text
wake up
do a happy gesture
wiggle your antennas
look left
say hello
turn on the light and nod
```

Other agents can send the same requests directly:

```python
await agent.send_to("reachy-mini", "do a happy gesture")
await agent.send_to("reachy-mini", "say hello")
```

## Use Reachy as the Wactorz interface

Use the explicit `ask Wactorz` prefix in chat to bypass Reachy's local robot planner:

~~~
@reachy-mini ask Wactorz what's the weather in Athens?
@reachy-mini ask Wactorz turn off the living-room light
@reachy-mini ask Wactorz summarize my calendar today
~~~

The prefix forwards the remaining text to main with `_via_interface=True`, so it uses
the same intent routing, agents, tools, and Home Assistant actuator path as normal
Wactorz chat. The final text is returned to chat and spoken through Reachy's speaker.
Without the prefix, Reachy handles its own robot, camera, speech, microphone, and
Home Assistant commands first; unhandled text can still fall through to main, but
`ask Wactorz` is the deterministic interface route.

The full path is:

~~~
text to @reachy-mini -> reachy-mini -> main -> selected Wactorz agent/tool
                    <- spoken and text answer <-
~~~

### Push-to-talk voice input

Voice input is an explicit, one-shot command. It records a bounded clip, transcribes
the WAV, sends the transcript through the same `_bridge_to_main` interface path above,
returns the answer to chat, and speaks it through Reachy:

~~~
@reachy-mini listen and ask Wactorz
~~~

Or over MQTT:

~~~json
topic: custom/reachy/cmd/ask_voice
payload: {"duration": 5}
~~~

`ask_voice` remains one-shot. There is deliberately no always-on microphone or wake
word.
The default backend is local `faster-whisper`; install and configure one backend before
using `ask_voice`:

~~~bash
# Local, recommended default
pip install faster-whisper
REACHY_STT_BACKEND=faster-whisper
REACHY_STT_MODEL=Infomaniak-AI/faster-whisper-large-v3-turbo

# Alternative local implementation
pip install openai-whisper
REACHY_STT_BACKEND=whisper
REACHY_STT_MODEL=base

# Hosted OpenAI transcription (explicit opt-in; audio leaves the host)
pip install 'wactorz[openai]'
REACHY_STT_BACKEND=openai
REACHY_STT_MODEL=gpt-4o-transcribe
OPENAI_API_KEY=...
~~~

Optional `REACHY_STT_LANGUAGE`, `REACHY_STT_FALLBACK_LANGUAGE`,
`REACHY_STT_DEVICE`, `REACHY_STT_COMPUTE_TYPE`, and `REACHY_STT_HOTWORDS`
settings tune language, uncertain-language fallback, local inference, and recognition
bias. Leave the primary language unset to auto-detect. Short utterances whose language
probability is below `stt_min_language_probability` (default `0.60`) are retried in
the configured fallback language; unresolved guesses are silently discarded instead
of being routed as commands. The MQTT payload can override these settings with
`stt_backend`, `stt_model`, `language`, `stt_fallback_language`, `stt_device`,
`stt_compute_type`, `stt_hotwords`, and `stt_min_language_probability`. Keys are read
from the environment and are never embedded in the recipe.

`custom/reachy/events` reports `transcript`, `capture_duration_s`,
`transcription_duration_s`, `response_text`, `total_duration_s`, `ok`, `error`,
`type`, and `ts`. Failures also carry a `stage`: `capture_failed`,
`transcription_failed`, `empty_transcript`, `routing_failed`, or `speech_failed`.
Completed-stage fields remain present on failures for diagnosis.

Before each clip the recorder drains a short, bounded queued WebRTC pre-roll. It
captures for the requested wall-clock duration even if the SDK yields buffered samples
faster than real time, and then retains only the newest requested-duration audio. This
prevents old queued audio from becoming the start of a new transcription.


### Opt-in conversation mode

Start a natural multi-turn session explicitly:

~~~
@reachy-mini start conversation
~~~

Or over MQTT:

~~~json
topic: custom/reachy/cmd/conversation_start
payload: {}
~~~

Main is the single conversational brain; the optional catalogue agent makes Reachy
its embodied microphone, speaker, and gesture surface. Home Assistant actions and
normal Wactorz tools follow Main's existing route. The current utterance is sent as
plain executable text, while
up to four prior turns travel separately as structured reference context. An older
command can therefore help resolve "it" without becoming part of the new command.

Reachy declares its available gestures in generic interface metadata. Main may return
a validated `<interface_action>` block for the catalogue agent to execute, so a
less-literal embodied request can still move the robot without a Main → Reachy
delegation loop. Wactorz core contains no Reachy dependency; without the catalogue
agent, Main behaves exactly as before.

Each turn uses voice-activity detection and ends after about one second of silence.
The complete reply appears in Reachy's dashboard thread before playback begins, and
the same sanitized reply is spoken in sentence-sized chunks without replacing its
ending with a "rest in Wactorz chat" notice. Recognized
speech stays in the same thread, labelled as an interface-mediated exchange; Main
remains internal reasoning metadata. Raw service calls and planner details remain
available in conversation diagnostics as `raw_response`, not ordinary chat.
Execution receipts such as `ran 4 of 4` are also hidden by default; say
`enable debug` to show them and
`disable debug` to return to the normal user-facing view. Debug always starts off
after an agent restart. Punctuation-only recognition noise is ignored.

Conversation sessions auto-detect the spoken language, so English and Greek can be
used without restarting the session. Set `stt_language` (or `REACHY_STT_LANGUAGE`)
only when you deliberately want to lock recognition to one language. Common names
and device terms are supplied as hotwords; override them with `stt_hotwords` or
`REACHY_STT_HOTWORDS`. Common mishearings such as "Richie", "Riti", "Ritzy", and
"Lizzy" are corrected to "Reachy" when used as the robot's name. Main is explicitly
told that these variants refer to Reachy, never the user, and it addresses the user
by name only after a clear naming statement or a durable saved fact. Local Faster
Whisper also enables its VAD, disables
previous-transcript conditioning, and reports confidence/no-speech scores. Results
below `stt_min_confidence` (default `0.25`) or above `stt_max_no_speech` (default
`0.60`) are silently discarded without consuming a turn.

Speech comes back out in whatever language the reply is written in. `TTS_VOICE`
sets the voice; a *Multilingual* edge-tts voice
(`en-US-BrianMultilingualNeural`, `en-AU-WilliamMultilingualNeural`, ...)
pronounces every supported language itself, so Reachy keeps one voice across
English and Greek. Any other voice speaks a single language and would read Greek
out as Unicode letter names, so Greek text is redirected to `TTS_VOICE_EL`
instead - `el-GR-AthinaNeural` (female) by default, or `el-GR-NestorasNeural`
(male); edge-tts ships no others. A per-call `{"voice": "..."}` on `say`
overrides both.

Confident auto-detected languages become a session hint for later ambiguous turns.
Voice-originated turns create no durable personal facts unless the user explicitly
says to remember or save something. This prevents a bad transcript from becoming the
user's name or household profile while preserving intentional voice memory.

Greek and English requests to lower or raise Reachy's own voice are handled locally
as robot speaker-volume changes rather than being sent to Home Assistant.

### Idle presets

Amplitude, tempo, which joints move and how often attract beats play are five
independent settings, and nobody wants to reason about five settings with an
audience already in front of the robot. A preset sets all of them at once:

| Preset | What it looks like |
|---|---|
| `off` | Completely still. Motors stay live and every command still works — this is stillness, not sleep. |
| `calm` | Barely moving, and *slower* as well as smaller. Breathing that is only shallower reads as a robot turned down; breathing that is also slower reads as something at rest. |
| `antennas` | Antennas alive, head and body held absolutely still. For a plinth where a sweeping head is a hazard, or when he should look like he is listening rather than performing. |
| `alive` | **The default.** Breathing, gaze drift, an attract beat every half minute or so. |
| `showtime` | Bigger and quicker, with beats two to three times as often. Too much for a quiet room, and meant to be. |

Set it whichever way is nearest to hand:

```json
{"cmd": "life", "preset": "showtime"}
```

```
REACHY_IDLE_PRESET=alive                        # .env, at boot
custom/reachy/config {"idle_preset": "calm"}    # persists
```

Or just say it — this is the control you reach for with people watching, so it
works out loud, in English or Greek:

> "calm down" · "settle down" · "antennas only" · "showtime" · "show off" ·
> "stop moving" · "alive" · "back to normal"
> "ηρέμησε" · "μόνο τις κεραίες" · "μη κουνιέσαι" · "πιο ζωηρά" · "κανονικά"

Naming a preset next to any word meaning *how you move* also works, so
"set preset animation alive", "idle showtime" and "motion calm" all land. A bare
preset name is matched only as the whole message: "alive" changes the preset,
"are you alive?" stays a question.

If ambient motion is ever held down by a flag that outlived whatever set it,
it releases itself after 25 seconds and logs which flag it was. Nothing
legitimate holds one that long, and the alternative — a robot that stops moving
permanently and silently while every command still reports success — is the
worst failure this feature has.

`{"cmd": "life"}` with no arguments reports the current preset and lists the
rest with a description of each. Individual settings are applied *after* a
preset, so `{"preset": "calm", "attract": true}` reads the way it looks: that
mood, with one deliberate exception.

### Ambient motion

A robot holding one pose perfectly still is hard to tell from a prop, which
matters most in a room where people are walking past. `REACHY_IDLE_LIFE=1` (or
`{"cmd": "life", "enabled": true}`, or `custom/reachy/config {"idle_life":
true}`) keeps a small amount of motion going: breathing on a 4.3s cycle, a slow
weight shift on 11.3s, gaze that settles somewhere for a few seconds and then
moves, and an occasional asymmetric antenna flick. The three periods do not
divide into each other, so the sum never visibly repeats. Off by default.

It cannot take the robot away from you, because it never names an absolute
target. Every offset is added to the pose your last command established, so a
`pose` moves the base and ambient motion breathes around the new one. It also
stands down completely while any command is in flight (`busy`), while Reachy is
speaking, and while he is asleep. Scale it with
`{"cmd": "life", "amplitude": 0.5}` or `REACHY_IDLE_LIFE_AMPLITUDE` — 1.0 is the
tuned default and 1.5 the maximum, and the hard ceilings in the code apply on
top of whatever you set.

A commanded pose is **held and then released**. It stays put for about three and
a half seconds, then eases back toward neutral over another two and a half. A
pose is transient intent, not a new resting posture: without this, aiming his
head down for a `describe` left him breathing politely at the floor until
something else moved him. The direction he is *facing* is never relaxed away —
turn him toward the room and he stays turned. Set `REACHY_IDLE_RELAX=0` or
`{"cmd": "life", "relax": false}` to pin a pose exactly where you put it.

After `look_at`, `look_pixel` or an `emotion` clip the head pose has no name in
pose space, so ambient motion leaves the head alone and keeps only the antennas
alive. He holds that gaze for the same few seconds, then makes one smooth
interpolated move back to neutral and resumes — a pause, never a dead end.

### Attract beats

Breathing stops him reading as switched off. It does not make anyone cross a
room. Every 18-45 seconds, when nothing else is happening, Reachy plays one
larger move drawn at random without immediate repeats: `scan` (a slow sweep of
the room), `perk` (head cock, antennas up), `double_take` (glance away, snap
back), `stretch`, `muse`. Each runs as a real trajectory under the motion lock
with `busy` set, so it yields to your commands exactly as ambient motion does,
and ends at neutral.

They are suppressed while Reachy is mid-turn in a conversation — a big move
while someone is being listened to reads as not paying attention. A session that
is merely open and waiting is fine. Turn them off with `REACHY_ATTRACT=0` or
`{"cmd": "life", "attract": false}`, retime with `REACHY_ATTRACT_MIN_GAP` /
`REACHY_ATTRACT_MAX_GAP`, or fire one on demand to check it reads from where the
audience will stand:

```json
{"cmd": "life", "beat": "perk"}
```

When ambient motion is on, spoken replies are animated against their own word
timings: edge-tts reports the offset and duration of every word it synthesises,
so accents land on the words rather than on a timer that would drift against the
sentence within a couple of seconds. A small lift at the start of an utterance
and a settle at the end give it a beginning and an end. Opt out for one line
with `{"cmd": "say", "text": "...", "speech_motion": false}`.

Emoji never reach the synthesiser. edge-tts does not skip them — it reads their
Unicode names aloud, so a cheerful reply ended with Reachy solemnly announcing
"smiling face with smiling eyes". They are stripped at synthesis, so every path
is covered: a direct `say`, a spoken vision description, or a reply the planner
wrote. Dashes, curly quotes and ellipsis are deliberately kept; they belong in
speech.

Audio is intentionally plainer than the dashboard response. Emoji, Markdown
role-play directions such as `*waves*`, links, and raw Home Assistant service/entity
syntax stay visual. Reachy speaks a short human acknowledgement instead; for example,
`Done: light.turn_on -> light.main_light` becomes "Okay, the light is on", or
"Okay, the light is pink" when that was the request.

Reachy applies the compatible parts of Pollen's conversation tuning to the XVF3800
audio processor. A firmware-owned nonlinear-attenuation flag is intentionally left
untouched; the robot can force its readback even after applying all useful
echo/noise settings. On some physical robots the microphone still hears the entire
speaker utterance, so a simple VAD monitor cannot reliably distinguish Reachy's
voice from a human interruption. Automatic barge-in therefore defaults off and
normal replies play to completion.

Set `"barge_in": true` only as an experiment on hardware with verified acoustic echo
cancellation. In chat, `start conversation with interruption` (also `start interruptible
conversation` or `start conversation with barge in`) sets the same flag without hand-writing
JSON; plain `start conversation` leaves it off. Either way the reply to the start command
says which mode you are in, so a robot that will not be talked over is distinguishable from
one that is ignoring you. In that mode the monitor ignores the first 450 ms of speaker startup,
then requires about 210 ms of sustained voice onset. Onset is treated as a suspicion
rather than a verdict: the sentence in progress finishes, and the recording is then
checked before it counts. It has to carry at least `barge_verify_min_speech_s`
of voice — defaulting to `barge_min_speech_s`, the same floor used to call it an utterance
in the first place, since a stricter one here only discards real interruptions — transcribe
to something the recogniser stands behind, and not repeat the words
Reachy was speaking — his own voice satisfies every acoustic test, so the only thing that
separates it from a person's is what was said. That check transcribes with the recogniser's own VAD switched off
(`stt_vad_filter`): the clip was recorded while the loudspeaker was playing and has already
been gated by Reachy's VAD, and running a second one over it discards the speech it was
meant to protect. The check runs alongside the next sentence rather than
between sentences: it costs a recogniser pass — seconds on the robot — and Reachy's own
voice trips the onset on nearly every sentence, so waiting for it put that pause into every
gap. One check runs at a time, and its answer is taken as soon as the sentence in progress
ends. A confirmed interruption stops the reply and is routed as the next turn,
carrying the transcript already made for the check rather than deriving it twice; an
unconfirmed one is discarded and he keeps talking.
The cost of a real interruption is the rest of one sentence, which reads as letting him
finish rather than as ignoring you. A typed `stop`,
`silence`, `quiet`, or `shut up` still cuts the current reply immediately without
ending the conversation. Spoken stop phrases work while Reachy is listening, but
reliable spoken interruption during TTS requires a true full-duplex realtime audio
pipeline rather than this cascaded STT -> LLM -> TTS path.

Stop with `stop conversation`, `stop the conversation`, `stop listening`,
`end conversation`, `end the conversation`, `goodbye Reachy`, `goodbye`, `bye`,
`that's all`, `σταμάτα`, `σταμάτα να ακούς`, `τέλος συζήτησης`, or `αντίο`.
You can also stop from chat or MQTT at any stage:

~~~
@reachy-mini stop conversation
~~~

~~~json
topic: custom/reachy/cmd/conversation_stop
payload: {}
~~~

Only one conversation can run at a time. By default it listens until a spoken stop
phrase or `conversation_stop`; set a positive `inactivity_timeout` to add an idle
timeout. Set a positive `max_turns` only when a bounded session is wanted. Optional
start fields include `silence_s`, `max_utterance_s`, `min_speech_s`, `pre_roll_s`,
`flush_s`, `vad_mode`, `vad_min_rms`, `cooldown_s`, `barge_in`, `barge_guard_s`,
`barge_onset_s`, `barge_silence_s`, `barge_min_speech_s`, `barge_flush_s`,
`barge_min_rms`, `barge_verify_min_speech_s`, `voice_friendly`,
`state_motion`, `idle_motion`, `stt_language`, `stt_hotwords`,
`stt_min_confidence`, `stt_max_no_speech`, and `max_turns`. Barge-in defaults to
false and is enabled only by an explicit `barge_in:true`; all physical conversation
motion defaults to false.

Set `state_motion:true` for subtle listening/speaking antenna cues. These use
antenna-only `set_target` calls and do not command or reset the head. Conversation
states are published on `custom/reachy/events`; events also report `session_id`,
`turn_index`, `transcript`, `response`, `raw_response`, `spoken_response`, `interrupted`,
timings, `stop_reason`, `ok`, `error`, and `ts`.

Physical idle motion defaults to `false` because Reachy's antenna servos are audible to
its live microphone and can become convincing Whisper hallucinations. The robot stays
mechanically still while recording; personality remains in deliberate response
gestures. Set `idle_motion:true` only to experiment on hardware with quiet servos. The
opt-in motion uses small, eased antenna sweeps, stops when voice activity is confirmed,
and never moves the head. Short mechanical bursts that trip VAD are rejected before
Whisper and do not consume a turn or error budget, but keeping motors still is the
reliable default.

Embodied requests stay on the robot. "Turn left" and "turn right" rotate the body
45 degrees relative to its current heading; an explicit angle such as "turn left
90 degrees" overrides the default within Reachy's safe range. "Do a little dance",
"turn around", "nod",
"shake your head", "wiggle your antennas", and "look curious" run safe, explicit
physical poses instead of going to the Wactorz LLM for pretend role-play. "What do
you see?" and other room-view questions make Reachy scan with its own camera; requests
specifically about the view in front capture a single forward frame. The dance ends
with a short spoken "Ta-da!"; the full action result remains visible in chat.

The Reachy playback API remains fire-and-forget, so non-interrupted turns use Edge
TTS word-boundary duration plus a tail pad and cooldown. Explicit stop and barge-in
both cut playback immediately. A plain chat "stop" is treated as "stop talking" and
produces no acknowledgement bubble unless debug mode is enabled. Local transcription
already running inside a native
STT library cannot be forcibly terminated, but its result is discarded after session
cancellation.


## Structured commands

For direct control, send a dict with `cmd`:

| Command | Purpose |
|---------|---------|
| `wake`, `sleep`, `stop` | Basic robot state |
| `pose` | Head yaw, pitch, roll, x/y/z |
| `antennas` | Left and right antenna angles |
| `gesture` | Physical `dance`, persistent `turn_around`, `nod`, `shake`, `wiggle`, or `curious` choreography |
| `face_forward` | Return from a rear-facing orientation to the user |
| `look_at`, `look_pixel` | Gaze target |
| `camera` | Capture one still frame from the onboard camera (base64 JPEG/PNG) |
| `describe` | Look through the camera and speak a description of the scene (vision LLM) |
| `look_around` | Scan several camera angles and describe the surrounding room |
| `look_behind` | Turn rearward, describe that camera view, and remain rear-facing |
| `debug` | Opt in or out of action-sequence receipts (`enabled: true/false`) |
| `listen` | Record a short mic-array clip (base64 WAV) with direction of arrival |
| `ask_voice` | Push-to-talk WAV → STT → main Wactorz route → spoken answer |
| `conversation_start`, `conversation_stop` | Opt-in VAD multi-turn Wactorz interface |
| `doa` | Report the mic array's current direction of arrival, no recording |
| `emotion`, `list_emotions` | Recorded gesture clips |
| `say`, `volume` | Speech and speaker volume |
| `life` | Ambient idle motion on/off, and its amplitude |
| `ha` | Home Assistant request |
| `bind`, `unbind` | Persistent reaction to an MQTT/HA event |

Examples:

```json
{"cmd": "wake"}
```

```json
{"cmd": "pose", "yaw": 30, "duration": 0.6, "method": "minjerk"}
```

```json
{"cmd": "antennas", "left": 45, "right": -45, "duration": 0.3}
```

## Camera and microphone

Reachy's onboard camera and microphone array are exposed as commands. The captured
bytes come back in the command result (and, optionally, on a one-shot MQTT topic or
saved to a file) — they are never written into the retained `custom/reachy/state`
heartbeat.

Capture a still frame (returns `image_b64`, plus `width`/`height`):

```json
{"cmd": "camera"}
```

```json
{"cmd": "camera", "format": "png", "path": "/tmp/shot.png", "publish": true}
```

Record a short mic clip (returns `audio_b64` WAV, `samplerate`, `channels`,
`duration_s`, and a best-effort `doa_deg`):

```json
{"cmd": "listen", "duration": 3}
```

Direction of arrival only, without recording:

```json
{"cmd": "doa"}
```

These commands require a video/audio-capable media backend and that the daemon holds
the camera and microphone (no Hugging Face app running on the robot).

> camera does not save the frame anywhere by default — the image comes back as
> base64 in the command result. Pass path to write it to disk or publish to
> emit it on custom/reachy/camera.

## See and describe

`describe` captures a frame, sends it to the vision-capable LLM, and **speaks the
real description** — unlike `camera` + `say`, which would only capture a frame and
then make up a line. Plain English "what do you see?", "what's in front of you?",
"describe the scene", and "look around" all route here. "Behind you" and "look
behind you" use the dedicated `look_behind` path: Reachy turns to a mechanically safe
155-degree rear view, captures without recentering, and stays there. Say "face me" or
"turn back" to return forward.

```json
{"cmd": "describe"}
```

Ask a specific question about the view, or get the text without speaking it:

```json
{"cmd": "describe", "question": "how many people are here?"}
```

```json
{"cmd": "describe", "say": false}
```

This needs the same video-capable media backend as `camera`, plus a configured LLM
provider (the model must support images).

## Troubleshooting

- If Wireless does not connect, check that the robot and Wactorz host are on the same LAN.
- If discovery fails, pin `robot_host` with `custom/reachy/config`.
- If motion commands do nothing, check that another app is not already controlling the robot.
- For Lite, restart `reachy-mini-daemon`, then restart `reachy-mini`.
