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
pip install reachy-mini numpy edge-tts
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
| `doa` | Report the mic array's current direction of arrival, no recording |
| `turn_to_sound` | Turn the head toward the direction the mic array localizes a sound (needs motors) |
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

## Turn toward a sound

The mic is an array, so it can estimate the direction a sound came from. `turn_to_sound`
reads that direction and turns the head toward it. Plain English "turn toward the sound",
"face the speaker", and "who's talking?" route here.

```json
{"cmd": "turn_to_sound"}
```

```json
{"cmd": "turn_to_sound", "require_voice": true, "duration": 0.6}
```

The *sensing* works whenever the mic is live, but the head turn needs the motors. The
array's zero-reference and rotation sense are robot-specific — if the head turns the wrong
way, calibrate once with `offset_deg` and/or `invert`:

```json
{"cmd": "turn_to_sound", "offset_deg": 0, "invert": false}
```

These require a video/audio-capable media backend and that the daemon holds the
camera and microphone (no Hugging Face app running on the robot). Plain English
also works: `take a photo`, `listen`.

> `camera` does not save the frame anywhere by default — the image comes back as
> base64 in the command result. Pass `path` to write it to disk or `publish` to
> emit it on `custom/reachy/camera`.

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
