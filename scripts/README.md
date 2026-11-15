# scripts/

Build and maintenance scripts.

| | |
| --- | --- |
| `build.py` | Package build entry point |
| `build_docs.py` | Renders `docs/*.md` into `static/docs/` |
| `sync_versions.py` | Propagates a version across the manifests |
| `sync_command_docs.py` | Writes the command table in `docs/reference.md` from what `/help` lists (`--check` to test it) |
| `gen_ha_icons.py` | Generates the Home Assistant add-on icons |
| `reachy_sim_check.py` | Runs the Reachy Mini agent against the SDK's simulated robot; no hardware needed (`--recovery` also tests reconnecting) |
| `hooks/` | Hatch build hooks |
| `start.ps1`, `start.bat` | Windows launcher: checks Python 3.11+ and `.env`, starts the authenticated broker, installs into the checkout's `.venv` and runs Wactorz (`start.bat` is the double-click wrapper; see `docs/quickstart.md`) |
| `watch-costs.ps1` | Windows: follows the LLM spend |

See [docs/quickstart.md](../docs/quickstart.md) for running Wactorz itself.
