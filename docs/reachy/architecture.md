# Architecture

How a spoken sentence becomes robot behaviour, and which code does each part. For how to
change it, see the [Developer guide](developer-guide.md).

## The pieces

```text
 ┌──────────── computer running Wactorz ─────────────────────────────────────────────┐
 │                                                                                    │
 │  Dashboard (browser) ──WebSocket──► monitor server ──► main agent ──► other agents │
 │                                        │                  ▲   │       (HA, planner,│
 │                                        │ @reachy-mini      │   │        catalogue…) │
 │                                        ▼                  │   ▼                    │
 │  MQTT broker ◄──custom/reachy/*──► reachy-mini agent ─────┘  LLM provider (cloud   │
 │  (Mosquitto)                        (catalogue program)       or Ollama)           │
 │                                        │                                           │
 │                         reachy_mini SDK (Pollen Robotics, pinned 1.8.4)             │
 └────────────────────────────────────────┼───────────────────────────────────────────┘
                     HTTP + WebSocket (motion, status, logs) │ WebRTC (camera, mic, speaker)
                                         ▼
                      Reachy Mini daemon (on the Wireless robot, or on the
                      computer for a Lite) ──► motors, camera, mic array, speaker

 Cloud services used by the agent:  Microsoft Edge TTS (speech out, always)
                                    Deepgram or OpenAI (speech in, if selected)
```

- **Wactorz** is required. It supervises the agent, runs the broker connection, hosts the
  dashboard, routes questions to the main agent and the language model, and owns Home
  Assistant access.
- **`reachy-mini`** is a *catalogue agent*: a Python program stored as a string
  (`AGENT_CODE`) and run inside a Wactorz `DynamicAgent` when someone sends
  `@catalog spawn reachy-mini`. It is restored automatically after a restart.
- **The SDK** (`reachy_mini`, by Pollen Robotics) is a dependency, not part of this
  repository. The agent talks to it only through the `ReachyMini` object.
- **The daemon** is Pollen's server that drives the hardware. It runs on a Wireless robot,
  or on the computer for a Lite (`reachy-mini-daemon -p <port>`).

## The voice pipeline

```text
 Visitor speaks
   │
   ▼ WebRTC microphone stream (16 kHz), read in a worker thread
 Voice activity detection: reachy_vad.capture_utterance()
   │  WebRTC VAD + loudness floor; pre-roll so the first syllable is kept;
   │  rejects short mechanical clicks; ends the turn after silence_s
   │  (streaming: frames are also sent live to Deepgram as they arrive)
   ▼
 Speech recognition: reachy_stt
   │  Deepgram streaming result, or a prerecorded fallback on the same audio;
   │  or local faster-whisper / whisper; or OpenAI.
   │  Low-confidence or wrong-language results are retried or discarded silently.
   ▼
 Transcript shown in chat under Reachy
   │
   ├─ a stop phrase ("goodbye")  → session ends
   ├─ a speech request ("say X loudly") → spoken directly
   ├─ an embodied request ("nod", "what do you see") → robot command, no LLM
   └─ anything else → _bridge_to_main(): send_to("main", {_via_interface: True, …})
                        main classifies, may delegate to agents / Home Assistant,
                        answers text; may return a validated gesture action
   ▼
 Reply shown in chat, then spoken
   │  sanitised for speech (no emoji, links, entity ids), split into sentences;
   │  each sentence synthesised by edge-tts while the previous one plays
   ▼
 Robot speaker (daemon /api/media/play_sound) ── optional speech-matched motion
   │
   ▼
 Listen again (after cooldown_s), until a stop phrase, inactivity or errors
```

Push-to-talk (`ask_voice`) is the same path with a fixed-length recording instead of
voice-activity detection and a single turn.

Typed chat enters at `handle_task()`: fixed phrases map straight to commands, other robot
or home requests go to a small LLM planner (`_nl_to_commands`) that emits a JSON list of
commands, and everything else takes the same `_bridge_to_main()` route.

## Commands and state

Every command, whatever its source, goes through one dispatcher, `_dispatch(agent, cmd,
payload)`. Sources:

| Source | Entry |
| --- | --- |
| Dashboard chat, other agents (`send_to`) | `handle_task()` |
| MQTT `custom/reachy/cmd` and `custom/reachy/cmd/<verb>` | subscriptions made in `setup()` |
| Reactive bindings (`bind`) | a subscription per bound topic, persisted |
| Voice conversation | `_conversation_loop()` |

Each command publishes its outcome on `custom/reachy/events` (and
`custom/reachy/cmd_result/<id>` when the payload has an `id`). `process()` republishes
`custom/reachy/state` every 5 seconds (retained). Runtime configuration arrives on
`custom/reachy/config`.

## Concurrency model

- Everything runs on Wactorz's single asyncio event loop. **Every blocking SDK call** goes
  through `_do()` (the default thread-pool executor), because a blocking call on the loop
  would stall every agent in the process, MQTT keep-alive included.
- Microphone capture and VAD run in a worker thread with a `threading.Event` for
  cancellation (`_conversation_blocking_worker`).
- **One interpolated motion at a time:** `motion_lock` (an `asyncio.Lock`) serialises
  `goto_target` moves; `busy` is set while one runs. Ambient motion streams small
  `set_target` offsets and stands down whenever `busy`, speaking, asleep or disconnected.
  `stop` deliberately does not take the lock, so it can interrupt.
- Background tasks (ambient life, attract beats, motor-link recovery, motor-fault log
  watch, the conversation loop) are started with `agent.run_in_background()`, which ties
  them to the agent's lifetime, and are cancelled in `cleanup()`.
- At most one conversation session exists at a time; starting a second is refused.

## Connection and recovery

- `setup()` never raises for a missing robot: the agent stays alive "disconnected",
  answers help and Home Assistant requests, and retries the motor link in the background.
- `_open_robot()` tries an ordered list of connection attempts built from
  `connection_mode`, `robot_host` and `media_backend`. If the robot is reachable but this
  computer cannot build the WebRTC media link (Linux without `gst-plugins-rs`), it
  connects without media and refuses camera/microphone commands with an explanation.
- A motor-link failure (`Lost connection with the server`, task timeouts) marks the link
  down and starts `_motion_reconnect_loop()`, which waits for speech to end, tries a fast
  control-socket-only reconnect, then a full reconnect, with exponential backoff up to
  30 seconds.
- A dropped WebRTC link during speech or listening triggers one full reconnect, at most
  every 15 seconds.

## Shutdown

`cleanup()` cancels ambient motion and recovery tasks, stops any conversation (which
cancels its capture thread through its event), closes the SDK handle, and saves reactive
bindings. It deliberately does not send the robot to sleep: doing so disables the motors
and the daemon may drop the next connection.

## Where the code is

| File | Responsibility |
| --- | --- |
| `wactorz/catalogue_agents/reachy_mini_agent.py` | The whole agent program (`AGENT_CODE`): connection, commands, speech, voice sessions, ambient motion, help. |
| `wactorz/catalogue_agents/reachy_stt.py` | Speech-recognition backends, configuration and the preflight check. |
| `wactorz/catalogue_agents/reachy_vad.py` | Microphone capture with voice-activity detection. |
| `wactorz/agents/catalog_agent.py` | The `reachy-mini` catalogue entry: install list, docs, input schema, task timeout. |
| `wactorz/agents/main/` | Main agent; handles `_via_interface` requests from Reachy. |
| `scripts/reachy_sim_check.py` | Runs the agent against the SDK's simulated robot. |
| `tests/test_reachy_*.py` | Hardware-free tests (fakes for the SDK, network and speech services). |
