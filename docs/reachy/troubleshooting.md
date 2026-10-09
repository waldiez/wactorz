# Troubleshooting

Find the symptom, then work down its list. Most problems are one of: the robot is not on
the same network, another program is controlling it, or a key or package is missing.

First, two commands that answer most questions:

```text
@reachy-mini health
@reachy-mini diagnostics
```

The Wactorz terminal (or `wactorz.log`) has the detail behind every message.

## Reachy does not connect

`health` says "I'm not connected to my body", or commands answer "reachy not connected".

1. Is the robot on? Wireless: wait until it has joined Wi-Fi. Lite: is the USB cable in,
   and is the `reachy-mini-daemon -p <port>` terminal still running without errors?
2. Is anything else controlling it? Close the Reachy Mini desktop app and stop any
   Hugging Face app on the robot. Only one program can drive Reachy.
3. Wireless: are the robot and computer on the **same** network? Guest and "IoT" networks
   often isolate devices. Try `ping reachy-mini.local` or the robot's IP address.
4. Wireless: set the address explicitly in `.env`, then restart Wactorz:

   ```dotenv
   REACHY_CONNECTION_MODE=network
   REACHY_ROBOT_HOST=192.168.1.42
   ```

   Always needed from WSL or a virtual machine, which cannot discover the robot.
5. Send `@reachy-mini reconnect`. No restart is needed once the robot is reachable.

If the robot was off when Wactorz started, Reachy keeps retrying in the background and
also reconnects when asked.

## Reachy talks but does not move

- Another program holds the motors: close the desktop app, then `reconnect`.
- The motors are switched off: send `enable motors`.
- The link to the motors dropped while speech still works: commands answer "My motor link
  dropped and I'm reconnecting on my own". Wait a few seconds; it recovers by itself.
- Still stuck: `reconnect force`, then `diagnostics`, which compares the robot's software
  version with this computer's and makes a test move.

## Reachy moves but does not speak

- Speech needs internet access on the computer running Wactorz (the voice is synthesized
  online). A message about the speech service sending nothing means the connection
  stalled.
- Speech coming out of the computer instead of the robot: set
  `REACHY_MEDIA_BACKEND=webrtc` in `.env` and restart Wactorz.
- It may be muted: `unmute`, then `normal volume`.

## Reachy's voice is quiet

- Say `presenter mode` (maximum robot volume).
- Install `ffmpeg` on the computer running Wactorz, then restart Wactorz. It adds a
  loudness boost; `health` says whether it is on.

  | System | Command |
  | --- | --- |
  | Windows | `winget install ffmpeg` |
  | macOS | `brew install ffmpeg` |
  | Debian, Ubuntu | `sudo apt install ffmpeg` |

## Voice input does not work

| Reachy says | Do this |
| --- | --- |
| "…needs an API key. Put DEEPGRAM_API_KEY in the .env file…" | Add the key, restart Wactorz. Or switch to local recognition: `REACHY_STT_BACKEND=faster-whisper` and `pip install faster-whisper`. |
| "…speech recognizer is not installed. Run: …" | Run the command it names in the Wactorz environment, restart Wactorz. |
| "voice detection is not installed…" | `pip install webrtcvad-wheels`, restart. |
| "Reachy is connected without its camera and microphone…" | Linux without GStreamer's WebRTC plugin. See [Installation › Linux](installation.md#linux). |
| "reachy recorded no audio…" | The microphone is held by another app on the robot. Close it and `reconnect`. |
| "I stopped listening because several voice turns in a row failed: …" | The reason is in the message, often a rejected key or no internet. Fix it, then `start conversation`. |
| "I stopped listening because nobody spoke for a while." | The conversation's idle limit was reached. `start conversation` again. |

The dashboard's mic button:

| What you see | Do this |
| --- | --- |
| No mic button | The browser can neither record nor recognize speech, or voice input is set to Off in the audio settings. Open the dashboard as `http://localhost:8888` (not an IP address) or over HTTPS. |
| "This browser has no speech recognition…" | Use Chrome or Edge, or switch voice input to Server. |
| "The server has no speech recognizer configured…" | Set `DEEPGRAM_API_KEY` (or the local recognizer) and restart Wactorz, or switch voice input to Browser. |
| "Microphone permission denied…" | Allow the microphone for the page in the browser's address bar. |

Reachy hears you but understands the wrong words:

- Move within a metre and face the robot. Reduce background noise.
- Turn idle motion off (`stop moving`): the microphone can hear the motors.
- Set the language when only one is spoken: `REACHY_STT_LANGUAGE=en` and
  `REACHY_STT_STREAM_LANGUAGE=en`.
- Add names it keeps mishearing to `REACHY_STT_HOTWORDS`.

Reachy answers its own voice, or stops mid-sentence: start conversations without
interruption (plain `start conversation`). Interruption is off unless asked for.

## The camera does not work

- "what do you see?" fails with an LLM message: your model must accept images, and
  `LLM_API_KEY` must be valid.
- No frame at all: another app on the robot holds the camera; close it and `reconnect`.
- On Linux the camera needs the same WebRTC plugin as the microphone.

## Spawning or installing fails

| Message | Do this |
| --- | --- |
| "…restart Wactorz once to finish; reachy-mini will start by itself after the restart" | Normal when the packages are installed at spawn. Restart Wactorz. Installing with the `reachy` extra avoids it. |
| "…some of its packages failed to install" | Fix what pip's error names (usually the network), then `@catalog spawn reachy-mini` again. |
| "This Python environment … has no pip" | `python -m ensurepip --upgrade`, or put `uv` on the `PATH`. |
| "the Python module '…' is not installed" | `@catalog spawn reachy-mini` again, or install the `reachy` extra. |

## Wactorz itself does not start, or the dashboard is empty

- "Connection refused" for MQTT: the broker is not running. Start it with
  `docker compose -f compose.dev.yaml up -d mosquitto`.
- "Not authorized" for MQTT: `MQTT_USERNAME`/`MQTT_PASSWORD` in `.env` differ from the
  broker's. With the bundled broker, recreate it after changing them:
  `docker compose -f compose.dev.yaml up -d --force-recreate mosquitto`.
- Settings in `.env` seem ignored: an installed (non-clone) Wactorz finds `.env` by walking
  up from its install folder. Keep the virtual environment inside the folder with `.env`,
  or set the variables in the environment.

## Messages that are not problems

| Message | Why it is harmless |
| --- | --- |
| `ERROR … No Reachy Mini Audio USB device found!` at startup, with a Wireless robot | The SDK first looks for a Lite's USB audio. |
| `Input Voltage Error` in the log | Reachy Mini runs its motors above their default threshold on purpose. It is never raised as a warning. |
| "I cannot currently read the live motor-fault monitor" | Only the Wireless robot serves its fault log, and only while connected. |
| "Reachy Mini doesn't provide a battery reading" | True of the hardware. Watch the LED on the base. |

## Reporting a problem

Include: what you typed or said, Reachy's reply, the output of `health` and
`diagnostics`, your operating system and robot model, and the lines of `wactorz.log`
around the time it happened. Remove API keys before sharing anything.
