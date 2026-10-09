# Configuration reference

Reachy reads its settings from three places, highest priority first:

1. **Runtime configuration over MQTT**, published to `custom/reachy/config`. Remembered
   across restarts, and overrides `.env`. Applied by the next `reconnect` (connection
   settings) or immediately (idle motion).
2. **The `.env` file** Wactorz reads, or real environment variables (which win over
   `.env`). Read when Wactorz starts: **edit `.env`, then restart Wactorz**, not only
   the agent.
3. **Per-command payload fields**, such as `{"cmd": "conversation_start", "barge_in": true}`,
   for that one command.

Secrets (API keys) are read only from the environment. They are never stored in the
agent's recipe, its saved state, or anything sent to the dashboard.

The template `.env.template` in the repository lists the common settings with comments.

## Connecting to the robot

| Setting | Default | Meaning |
| --- | --- | --- |
| `REACHY_CONNECTION_MODE` | auto | `network`: straight to a Wireless robot over Wi-Fi, never via this computer. `local`: the Reachy Mini desktop app, a Lite's daemon, or the simulator, on this computer. Empty: try this computer first, then the network. |
| `REACHY_ROBOT_HOST` | empty | The Wireless robot's IP address or `reachy-mini.local`. Set it whenever discovery is unreliable, and always from WSL or a VM. Ignored in `local` mode. |
| `REACHY_MEDIA_BACKEND` | SDK default | `webrtc` sends speech to the **robot's** speaker. Other backends play on this computer. Leave empty unless speech comes out of the wrong device. |

Over MQTT (`custom/reachy/config`), the same three are `connection_mode`, `robot_host`
and `media_backend`. Send `@reachy-mini reconnect` afterwards.

## Speech output

| Setting | Default | Meaning |
| --- | --- | --- |
| `TTS_VOICE` | `en-US-JennyNeural` | Any Microsoft Edge voice. A *Multilingual* voice (for example `en-US-BrianMultilingualNeural`) speaks English and Greek in one voice. |
| `TTS_VOICE_EL` | `el-GR-AthinaNeural` | Voice for Greek text when `TTS_VOICE` is single-language. `el-GR-NestorasNeural` is the male alternative. |

Speech volume is the robot's own speaker level, changed by command (`speak louder`,
`presenter mode`, `set volume to 80`) and remembered by the robot.

## Speech recognition

Used by `listen and ask Wactorz` and `start conversation`. Typed commands and Reachy's
own speech do not need it.

| Setting | Default | Meaning |
| --- | --- | --- |
| `REACHY_STT_BACKEND` | `deepgram` | `deepgram` (hosted), `faster-whisper` (local), `whisper` (local), or `openai` (hosted). |
| `DEEPGRAM_API_KEY` | empty | Required for `deepgram`. |
| `OPENAI_API_KEY` | empty | Required for `openai` speech recognition (separate from `LLM_API_KEY`). |
| `REACHY_STT_MODEL` | per backend | `nova-3` (Deepgram), `base` (both Whispers), `gpt-4o-transcribe` (OpenAI). |
| `REACHY_STT_LANGUAGE` | auto-detect | Language for recorded clips: `en`, `el`, or `multi` with Nova-3. |
| `REACHY_STT_STREAMING` | on | Stream conversation audio to Deepgram while you speak. Off sends each finished turn as one clip. |
| `REACHY_STT_STREAM_LANGUAGE` | `multi` | Language for streaming: `multi` (English and Greek), `en` or `el`. |
| `REACHY_STT_FALLBACK_LANGUAGE` | empty | Language to retry an uncertain short utterance in, for example `el`. |
| `REACHY_STT_HOTWORDS` | Reachy, Wactorz, Home Assistant, … | Comma-separated names the recognizer should favour. |
| `REACHY_STT_TIMEOUT_S` | `60` | Longest wait for a hosted recognizer. |
| `REACHY_STT_DEVICE` | `auto` | Local Whisper: `cpu`, `cuda` or `auto`. |
| `REACHY_STT_COMPUTE_TYPE` | `default` | faster-whisper precision, for example `int8` on a CPU. |

Before recording, Reachy checks that the selected recognizer is installed and has its key,
and says what is missing instead of listening.

### Conversation tuning

Passed as fields of `{"cmd": "conversation_start", ...}`. Defaults suit a quiet room.

| Field | Default | Meaning |
| --- | --- | --- |
| `inactivity_timeout` | `0` (never) | Seconds of nobody speaking before the session ends by itself. Useful at an exhibition stand. |
| `max_turns` | `0` (unlimited) | End after this many turns. |
| `silence_s` | `1.0` | Pause that ends your turn. Raise it for people who pause mid-sentence. |
| `max_utterance_s` | `20` | Longest single turn. |
| `vad_mode` | `2` | Voice-detector aggressiveness, 0 (lenient) to 3 (strict). Raise it in a noisy room. |
| `vad_min_rms` | `0.01` | Minimum loudness counted as voice. Raise it in a noisy room. |
| `barge_in` | `false` | Let people interrupt Reachy by talking. Experimental: the microphone hears Reachy's own speaker on some robots. |
| `cooldown_s` | `0` | Pause after Reachy speaks before listening again. |
| `state_motion` | `false` | Small antenna cues while listening and speaking (`REACHY_CONVERSATION_STATE_MOTION=1`). |
| `idle_motion` | `false` | Antenna sweeps while waiting (`REACHY_CONVERSATION_IDLE_MOTION=1`). The microphone can hear the servos. |

## Idle motion

Off by default: a still robot never moves without being asked.

| Setting | Default | Meaning |
| --- | --- | --- |
| `REACHY_IDLE_PRESET` | `off` | `off`, `calm`, `antennas`, `alive` or `showtime`. |
| `REACHY_IDLE_LIFE` | off | `1` turns ambient motion on with the `alive` preset. |
| `REACHY_IDLE_LIFE_AMPLITUDE` | preset's | Scale, up to `1.5`. Hard limits in the code apply on top. |
| `REACHY_IDLE_RELAX` | on | A commanded pose eases back to neutral after a few seconds. `0` holds it. |
| `REACHY_ATTRACT` | with preset | Occasional larger "attract" moves. |
| `REACHY_ATTRACT_MIN_GAP`, `REACHY_ATTRACT_MAX_GAP` | `18`, `45` | Seconds between attract moves. |

Change it live by saying `calm down`, `showtime` or `stop moving`, or by publishing
`{"idle_preset": "calm"}` to `custom/reachy/config`.

## Wactorz settings Reachy relies on

| Setting | Why |
| --- | --- |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY` | Answers to questions, the plain-English command planner, and camera descriptions. Descriptions need a model that accepts images. |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` | The broker every agent, Reachy included, communicates through. |
| `HA_URL`, `HA_TOKEN` | Optional. Home Assistant control through Reachy. |
| `MONITOR_PORT` | Dashboard port, `8888` by default. |
| `API_KEY` | Protects the dashboard and API. Set it before exposing Wactorz beyond this computer. |
