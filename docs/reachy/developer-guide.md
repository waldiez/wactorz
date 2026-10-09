# Developer guide

For engineers changing or extending the Reachy Mini integration. Read
[Architecture](architecture.md) first, and the repository's `AGENTS.md`, which sets the
conventions every change here follows.

## Development environment

```bash
git clone https://github.com/waldiez/wactorz.git
cd wactorz
git switch dev
uv sync --locked --extra all --extra dev --extra reachy
```

`make install-dev` installs everything except the `reachy` extra; add it as above when
working on Reachy. Branch from `dev` with a conventional prefix (`fix/`, `feat/`,
`docs/`, `chore/`), and open pull requests against `dev`, never `main`.

## Repository map for Reachy

| Path | What it is |
| --- | --- |
| `wactorz/catalogue_agents/reachy_mini_agent.py` | The agent program, as the string `AGENT_CODE`. |
| `wactorz/catalogue_agents/reachy_stt.py` | Speech-to-text backends (a normal, importable module). |
| `wactorz/catalogue_agents/reachy_vad.py` | Voice-activity capture (a normal module). |
| `wactorz/agents/catalog_agent.py` | The catalogue entry (`catalog["reachy-mini"]`): packages, docs, input schema, `task_timeout_s`. |
| `pyproject.toml` | The `reachy` extra and the uv override that keeps Starlette patched. |
| `tests/test_reachy_*.py` | The Reachy test suite. |
| `scripts/reachy_sim_check.py` | End-to-end run against the SDK's simulated robot. |
| `docs/catalogue-reachy-mini.md` | Full command reference, built into the docs site. |
| `docs/reachy/` | These guides. |

## What "catalogue agent" means for your code

`AGENT_CODE` is executed by Wactorz with `exec()` when the agent is spawned. Consequences:

- **Linters and the type checker do not read it.** Ruff and basedpyright see one string.
  `tests/test_catalogue_agent_code.py` at least parses it, so a syntax error fails a test.
  Be your own linter: run the Reachy tests, and read your diff.
- **Module-level names in `AGENT_CODE` are the agent's globals.** Tests exec it into a
  dict (`NS`) and patch entries in that dict to replace collaborators.
- The program defines up to three entry points Wactorz calls: `setup(agent)` once,
  `process(agent)` every `poll_interval` seconds (5), and `handle_task(agent, payload)`
  per chat or `send_to` message (bounded by the recipe's `task_timeout_s`, 140 seconds).
  Optional `cleanup(agent)` runs at stop.
- `agent` is the DynamicAgent API: `publish`, `subscribe`, `send_to`, `persist`,
  `recall`, `log`, `alert`, `notify_user`, `run_in_background`, `state` (a dict that is
  not persisted), and `llm`. See `docs/agents.md` › DynamicAgent.
- The program imports `wactorz.catalogue_agents.reachy_stt` and `reachy_vad`, so unlike
  most catalogue programs it **cannot run on a remote Wactorz node** that lacks the
  `wactorz` package. Run it on the main host.

## Rules that keep the robot safe and the process responsive

1. **Never call the SDK on the event loop.** Wrap every SDK call in `await _do(fn, ...)`.
   A blocking call freezes every agent in the process.
2. **Interpolated moves take `motion_lock` and set `busy`.** Look at `_pose()` for the
   pattern. Ambient motion relies on `busy` to stay out of the way.
3. **Clamp every angle** to the robot's safe range in the command, not only in the caller.
4. **Every outbound call has a timeout**, generous rather than tight: aiohttp
   `ClientTimeout`, `asyncio.wait_for`, the SDK's own timeouts.
5. **Failures raise; successes return a dict.** `_dispatch()` stamps `ok: true` on any
   returned dict, so a returned "failure" would be published as a success. Raise
   `_CommandStageError(stage, message, fields)` when the event should carry partial results.
6. **Every result carries a human sentence in `result`.** Chat shows it.
7. **Speak only what a person should hear.** Pass replies through `_voice_friendly_reply()`.

## Adding a command

1. Write `async def _mycommand(agent, payload) -> dict` near similar commands.
2. Add a branch in `_dispatch()`.
3. If it drives the SDK handle, add it to `_ROBOT_HANDLE_COMMANDS` (and to
   `_MOTION_COMMANDS` if it moves), so MQTT callers get a clear "not connected" while the
   robot is offline. If it needs the camera or microphone, add it to `_MEDIA_COMMANDS`.
4. Add the verb to the per-verb MQTT topic list in `setup()` if it should have
   `custom/reachy/cmd/<verb>`.
5. For a fixed spoken or typed phrase, add it to `handle_task()` or
   `_embodied_command_for_text()`; for planner access, document it in `_NL_SYSTEM`.
6. Add it to `input_schema` in `catalog_agent.py`, to the command table in
   `docs/catalogue-reachy-mini.md`, and to `_HELP_TOPICS` if users should discover it.
7. Test it (below).

## Adding a speech-recognition backend

In `reachy_stt.py`: implement a class with
`async def transcribe(self, wav_bytes, config) -> str | _BackendResult`, register it in
`_BACKENDS` and `_DEFAULT_MODELS`, add aliases to `_BACKEND_ALIASES`, and add its module,
install command and key variable to `_BACKEND_REQUIREMENTS` so the preflight check covers
it. Run blocking libraries with `asyncio.to_thread`. Honour `config.timeout_s` for any
network call.

## Supporting a new SDK version

The SDK is pinned (`reachy-mini==1.8.4`) in both `pyproject.toml` and
`catalog_agent.py`'s `_REACHY_MINI_SDK_VERSION`. To move it:

1. Change both pins, then `make lock`.
2. `uv sync --locked --extra all --extra dev --extra reachy`.
3. Run `uv run --no-sync pytest tests/test_reachy_sdk_compatibility.py`: it fails if the
   SDK stops providing a method the agent calls.
4. Run `uv run --no-sync python scripts/reachy_sim_check.py --recovery`.
5. Test on the robot (see the hardware checklist in
   [internal/test_report.md](internal/test_report.md)).

## Testing

| Tier | Command | Needs | Proves |
| --- | --- | --- | --- |
| Unit | `uv run --no-sync pytest tests/test_reachy_*.py` | nothing | Agent logic against fakes of the SDK, broker, speech services. |
| SDK contract | `uv run --no-sync pytest tests/test_reachy_sdk_compatibility.py` | `reachy` extra | The pinned SDK imports and provides every call the agent makes. |
| Simulation | `uv run --no-sync python scripts/reachy_sim_check.py --recovery` | `reachy` extra, port 8000 free | The agent drives the real SDK and daemon (simulated motors) through connect, motion, stop, reconnect and recovery. |
| Hardware | manual, see [test report](internal/test_report.md#hardware-checklist) | the robot | Everything else: motors, audio, camera, network, speech services. |

Unit tests never call paid services or need hardware: patch `NS["_prepare_speech"]`
(edge-tts), `reachy_stt.transcribe_wav` (recognizers) and `NS["_voice_input_problem"]`
(the preflight) as the existing tests do. Before a pull request, run `make lint` and
`make test`, and `uv run --no-sync ruff check` / `basedpyright` on what you touched.

## Extension points beyond Reachy

The integration is a reasonable template for other embodied devices:

- **Interface bridge.** `_bridge_to_main()` sends text to main with `_via_interface`,
  `_interface_source`, `_interface_context` (display name, kind, a capabilities list) and
  optional `_interface_history`. Main contains no Reachy-specific code; any device agent
  can use the same envelope and receive `interface_actions` limited to the capabilities
  it declared.
- **Speech in and out.** `reachy_stt` and `reachy_vad` take a `media` object with
  `start_recording`, `get_audio_sample`, `get_input_audio_samplerate`,
  `get_input_channels`, `stop_recording`. Another microphone that offers those methods
  can reuse them unchanged.
- **MQTT surface.** `custom/<device>/cmd|config|state|events` is the pattern; other
  agents and Home Assistant automations interact with the robot only through it.

What does not transfer yet: the speech output path (edge-tts plus the Reachy daemon's
play-sound API) and the motion code are Reachy-specific and live in the one
`AGENT_CODE` file. See [internal/technical_debt.md](internal/technical_debt.md) for the
suggested split.
