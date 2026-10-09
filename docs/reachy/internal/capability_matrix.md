# Capability matrix, test results and backlog (audit of 2026-10-09)

Branch `chore/reachy-release-readiness`, from `feat/reachy-deepgram-animations` @ `0c2ff2eb`.
SDK `reachy-mini==1.8.4`. **No physical robot was available for this audit.** "Sim" means
verified with `scripts/reachy_sim_check.py` against the SDK's own daemon in
`--mockup-sim --no-media` mode (real SDK and agent code, simulated motors, no audio/camera).

## Capabilities

| Capability | Status | Evidence / source |
| --- | --- | --- |
| Connect, bring-up, wake, sleep | Verified working (sim) | `_open_robot`, `_bring_up_robot` |
| Pose, antennas, look_at, nod, turn, face forward | Verified working (sim) | `_pose`, `_antennas`, `_gesture`, `_turn` |
| `stop` holds current pose | Verified working (sim); **was broken**: snapped head to neutral in 0.1 s (SDK has no `stop`) | `_stop`, fixed |
| Automatic motor-link recovery after daemon loss | Verified working (sim): recovered ~4–9 s after daemon returned | `_motion_reconnect_loop` |
| Offline commands over MQTT give a clear reason | Verified working (unit); **was broken** (`'NoneType' has no attribute`) | `_dispatch`, fixed |
| Ambient idle motion via `REACHY_IDLE_LIFE=1` / `life enabled` | Verified working (sim); **was broken**: reported on, robot stayed still | `_switch_life_on`, fixed |
| Health IMU temperature | Fixed, verified by SDK contract test only; **was broken** (called non-existent `get_imu_data`) | `_imu_temperature` |
| Voice-input preflight (missing key/package) | Verified working (unit), new | `reachy_stt.configuration_problem` |
| Conversation ends by itself → chat says why | Verified working (unit), new | `_announce_conversation_end` |
| Help, routing of typed phrases, MQTT surface | Verified working (unit) | `handle_task`, `_help` |
| Dashboard mic, Server engine (`POST /api/stt`) | Verified working (unit, both sides); not tried in a real browser | `wactorz/ext/stt`, `frontend/src/io/SpeechToText.ts`, `wav.ts` |
| Dashboard mic, Browser engine (Web Speech) | Verified working (unit); not tried in a real browser | `frontend/src/io/WebSpeech.ts`, `voiceInput.ts` |
| Speech output (edge-tts → robot speaker), volume | Implemented but untested on hardware this audit; now bounded by a 20 s stall timeout | `_prepare_speech`, `_say` |
| Push-to-talk, Deepgram streaming conversation, VAD | Implemented but untested on hardware this audit (reported working at public demos) | `_ask_voice`, `_conversation_loop`, `reachy_vad` |
| Local faster-whisper / whisper STT | Implemented but untested | `reachy_stt` |
| OpenAI STT | Implemented but untested; now honours `REACHY_STT_TIMEOUT_S` | `OpenAIBackend` |
| Camera capture, describe, look around | Implemented but untested; image format is Anthropic's; other providers Unknown | `_camera`, `_vision_describe` |
| Rear-facing body turn to 155° | Partially working: SDK 1.8.4 lacks public `goto_joint_positions`, so the IK fallback is always used and may stop short | `_face_body` |
| Direction of arrival | Partially working: readable (`doa`, `listen`), never used to orient the head | `_read_doa` |
| Barge-in (interrupt by voice) | Partially working, experimental, off by default (echo on some robots) | `_verified_barge_in` |
| "Connected" status after daemon dies | Partially working: stays "connected" until a motor command fails (sim) | `_is_connected` |
| Linux | Partially working: motion + speech only without `gst-plugins-rs` | `_MEDIA_UNAVAILABLE_NOTICE` |
| Home Assistant via Reachy, reactive bindings | Implemented but untested on hardware (unit-tested) | `_ha`, `_bind` |
| Docker `ultra` image / HA add-on | Unknown: docs say unsupported, images carry SDK build deps | Dockerfile, CHANGELOG |
| Remote Wactorz node | Not supported (program imports `wactorz.catalogue_agents.*`) | `reachy_mini_agent.py` |
| Wake word, sound-direction head turn, local TTS, presence greeting | Planned / proposed only | none |

## Tests run

| Command | Result |
| --- | --- |
| `pytest tests/test_reachy_*.py` (baseline, before changes) | 1046 passed (with catalogue tests) |
| `pytest tests -n 4` (after first fixes) | 7502 passed, 90 skipped |
| `pytest tests/test_reachy_*.py` (final) | 978 passed, 92 subtests |
| New tests failing on original code | 10 of 13 safety tests fail, TTS-stall test hangs forever |
| `scripts/reachy_sim_check.py --recovery` | 15/15 steps passed (sim) |
| ruff check/format, basedpyright on changed files | clean |
| `uv lock --check` | consistent |
| `uv audit` | **not run**: this uv (0.11.8) rejects the Makefile's preview flag |

Sim timings (mock motors, not meaningful for hardware): connect 6.2 s, wake 2.5 s, pose
0.5 s, nod 2.6 s, stop 0.36 s, reconnect 4.0 s. End-to-end voice latency: **not measured**.

## Hardware checklist (needs the owner)

1. Wireless: spawn, `health` (IMU temperature now appears), wake, `look left`, nod, dance.
2. `{"cmd":"stop"}` during a slow pose: head should freeze in place, not snap.
3. `REACHY_IDLE_LIFE=1` only: robot should breathe after start.
4. `say` with internet unplugged: reply should fail within ~20 s with a clear reason.
5. Without `DEEPGRAM_API_KEY`: `start conversation` refuses with the fix.
6. With key: 5-turn English + Greek conversation; time speech-end → reply-start.
7. Conversation with `inactivity_timeout: 20`: chat explains the end.
8. Pull Wi-Fi mid-session, restore: recovery without commands.
9. `turn around`: does the base reach ~155°?
10. Camera describe with the demo's LLM provider.

## Prioritised backlog

1. (P0) Run the hardware checklist above before any public demo of this branch.
2. (P1) Security: `path` fields on `camera`/`listen` let any MQTT client write files anywhere the process can; restrict to a directory (**needs approval: behaviour change**).
3. (P1) Lower transcript excerpts in logs to debug level (privacy at public events).
4. (P1) Detect a dead daemon without waiting for a failed command (read SDK client liveness off-loop).
5. (P1) Resolve the Docker/add-on support contradiction in docs.
6. (P2) Rear turn: use the SDK's joint interpolation for 1.8.4 or document the limit.
7. (P2) `.env` discovery for pip installs (`find_dotenv(usecwd=True)` fallback); core change, needs approval.
8. (P2) Edge-tts is an unofficial client of Microsoft's service; review terms before commercial use; consider a local TTS option.
9. (P2) Split `AGENT_CODE` (8k+ lines, unlinted) into importable modules like `reachy_stt`.
10. (P3) Sound-direction head turn; visitor greeting; dashboard listening indicator.

Still to write (not done in this session): `architecture_audit.md`, `technical_debt.md`,
`security_privacy_review.md`, `test_report.md`, `release_readiness.md`,
`REACHY_EXECUTIVE_BRIEF.md`. The content above is their basis.
