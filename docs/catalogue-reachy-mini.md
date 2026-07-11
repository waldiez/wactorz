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

## Troubleshooting

- If Wireless does not connect, check that the robot and Wactorz host are on the same LAN.
- If discovery fails, pin `robot_host` with `custom/reachy/config`.
- If motion commands do nothing, check that another app is not already controlling the robot.
- For Lite, restart `reachy-mini-daemon`, then restart `reachy-mini`.
