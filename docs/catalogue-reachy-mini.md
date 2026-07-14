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
pip install reachy-mini numpy edge-tts webrtcvad-wheels
```

`edge-tts` is required for the `say` command (speech synthesis); the other
commands work without it.

**Optional:** install `ffmpeg` (a system binary, not a pip package) on the host
if the robot's speech comes out too quiet. It is used only to boost the TTS
loudness by roughly 3-4x. Without it, `say` still works - it plays the raw,
quieter audio and logs a warning.

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
once and restart the agent:

```text
topic: custom/reachy/config
payload: {"robot_host": "192.168.1.42"}
```

Use the robot's IP address or hostname. The current host is reported in
`custom/reachy/state` as `robot_host`.

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

You can also set `REACHY_CONNECTION_MODE=network` (or `local`) in the environment.
Restart the agent to apply; the active mode is reported in `custom/reachy/state` as
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
REACHY_STT_MODEL=base

# Alternative local implementation
pip install openai-whisper
REACHY_STT_BACKEND=whisper
REACHY_STT_MODEL=base

# Hosted OpenAI transcription (explicit opt-in; audio leaves the host)
pip install 'wactorz[openai]'
REACHY_STT_BACKEND=openai
REACHY_STT_MODEL=whisper-1
OPENAI_API_KEY=...
~~~

Optional `REACHY_STT_LANGUAGE`, `REACHY_STT_DEVICE`, and
`REACHY_STT_COMPUTE_TYPE` settings tune language and local inference. The MQTT
payload can override them per request with `stt_backend`, `stt_model`, `language`,
`stt_device`, and `stt_compute_type`. Keys are read from the environment and are
never embedded in the recipe.

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

Start a bounded multi-turn session explicitly:

~~~
@reachy-mini start conversation
~~~

Or over MQTT:

~~~json
topic: custom/reachy/cmd/conversation_start
payload: {"inactivity_timeout": 30, "max_turns": 10}
~~~

Reachy uses the same STT provider and `_bridge_to_main` route as `ask_voice`, so
Home Assistant actions and normal Wactorz tools follow the existing path. Each turn
uses voice-activity detection and ends after about 0.8 seconds of silence. Reachy
then speaks the result, waits for the duration reported by TTS, and drains the
WebRTC microphone queue during a short cooldown before listening again. Previous
turns are supplied as context so follow-ups such as "turn it back on" can resolve
without duplicating routing logic.

Stop with any of these phrases: `stop listening`, `end conversation`, or `goodbye
Reachy`. You can also stop from chat or MQTT at any stage:

~~~
@reachy-mini stop conversation
~~~

~~~json
topic: custom/reachy/cmd/conversation_stop
payload: {}
~~~

Only one conversation can run at a time. A session also stops after its inactivity
timeout or maximum turn count. Optional start payload fields include `silence_s`,
`max_utterance_s`, `min_speech_s`, `pre_roll_s`, `flush_s`, `vad_mode`,
`vad_min_rms`, and `cooldown_s`. The default RMS floor rejects very quiet WebRTC
codec noise. Events on `custom/reachy/events` have `type: conversation` and report
`session_id`, `turn_index`, `state`, `transcript`, `response`, capture/transcription/
routing timings, `stop_reason`, `ok`, `error`, and `ts`.

The current Reachy WebRTC playback API is fire-and-forget: it does not expose a
remote playback-complete callback. Conversation mode therefore gates the microphone
using the Edge TTS word-boundary duration plus a tail pad and cooldown. An explicit
stop still interrupts playback immediately. Local transcription work already running
inside a native STT library cannot be forcibly terminated, but its result is discarded
and never routed after session cancellation.


## Structured commands

For direct control, send a dict with `cmd`:

| Command | Purpose |
|---------|---------|
| `wake`, `sleep`, `stop` | Basic robot state |
| `pose` | Head yaw, pitch, roll, x/y/z |
| `antennas` | Left and right antenna angles |
| `look_at`, `look_pixel` | Gaze target |
| `camera` | Capture one still frame from the onboard camera (base64 JPEG/PNG) |
| `describe` | Look through the camera and speak a description of the scene (vision LLM) |
| `listen` | Record a short mic-array clip (base64 WAV) with direction of arrival |
| `ask_voice` | Push-to-talk WAV → STT → main Wactorz route → spoken answer |
| `conversation_start`, `conversation_stop` | Opt-in VAD multi-turn Wactorz interface |
| `doa` | Report the mic array's current direction of arrival, no recording |
| `emotion`, `list_emotions` | Recorded gesture clips |
| `say`, `volume` | Speech and speaker volume |
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
"describe the scene", and "look around" all route here.

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
